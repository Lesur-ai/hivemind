# -*- coding: utf-8 -*-
"""
Cycle de vie d'un space Hivemind (issue #5) : détection, membership, bootstrap,
resync et surface de statut read-only.

Ce module construit les primitives de cycle de vie au-dessus de l'état #3
(``HivemindStateStore``) et du canal pair #4. Il **ne** fait PAS : l'ordre de
queue (#6), les leases/fencing de token (#7), le staging/commit de bank (#8),
la protection des mutations (#9) ni la réplication de live-notes (#12). Il pose
seulement la fondation membership/epoch/bank_version/resync que ces issues
consomment.

Invariants de sûreté portés ici :

- **Détection fail-closed** : un space est « Hivemind » si ``node.json`` se
  désérialise ET ``members.json`` a au moins un membre ACTIVE. Une corruption
  de ces fichiers critiques remonte comme ``CorruptedStateError`` (donc
  unsafe/bloqué) — JAMAIS comme « non partagé ». Un downgrade silencieux =
  bypass de token = split-brain.
- **Epoch monotone & explicite** : ``MembershipService`` est le SEUL endroit
  qui bumpe l'epoch (add = +1, éviction confirmée = +1). On n'avance jamais la
  composition partagée depuis un message entrant.
- **Import transactionnel** : ``node_status`` passe à ``UNSAFE`` avant tout
  write, on vérifie le manifest + chaque sha256 par-fichier, on écrit via
  get/put (pas copy_object — les checksums se vérifient en-process), on frappe
  un node_id local NEUF, on préserve l'epoch + bank_version source, puis on
  re-vérifie l'égalité bank_version avant de passer ``HEALTHY``. Tout échec
  laisse ``UNSAFE``.
- **Santé node-local** (``node_status.json``) strictement séparée de
  ``Member.status`` partagé.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from ..models import meta_shared_projection
from ..storage import StorageService
from . import layout
from .models import (
    BootstrapManifest,
    BootstrapManifestEntry,
    BankVersionPointer,
    CorruptedStateError,
    EventEnvelope,
    EventType,
    HiveNodeStatus,
    Member,
    MemberStatus,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    TermState,
    TokenLeaseState,
    TokenState,
)
from .state import HivemindStateStore
from .peer import PeerChannelError, _load_public_key
from ..reservation_guard import assert_no_pairing_activation


# =============================================================================
# Exceptions de cycle de vie
# =============================================================================


class BootstrapError(RuntimeError):
    """Erreur de cycle de vie bootstrap/membership/resync (fail-closed)."""


class BootstrapLimitError(BootstrapError):
    """A bounded bootstrap inventory or payload limit was exceeded."""


class MembershipIncarnationError(BootstrapError):
    """Compare-and-evict a échoué : le membre courant n'a pas l'incarnation
    attendue (le node a été ré-enrôlé depuis l'appariement). Fail-closed (P10-3)."""


class MembershipEpochError(BootstrapError):
    """Compare-and-mutate a échoué : l'epoch membership courant ne correspond pas
    à ``expected_epoch`` (une mutation concurrente — ex. re-scope — a avancé
    l'epoch entre le contrôle et l'admission/promotion). Fail-closed (P10-3)."""


# =============================================================================
# Hash canonique déterministe (aligné sur peer.canonical_event_payload_hash)
# =============================================================================


def _canonical_json_bytes(value: Any) -> bytes:
    """JSON canonique : clés triées, séparateurs compacts, UTF-8.

    Identique à ``peer._canonical_json_bytes`` pour garder un hash
    déterministe cross-host quel que soit l'ordre d'insertion des clés.
    """
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def manifest_content_hash(manifest: BootstrapManifest) -> str:
    """Hash de couverture du manifest : sha256 du JSON canonique des HEADERS
    critiques (protocol_version, source_node_id, membership_epoch, bank_version,
    commit_id) ET des paires ``(path, sha256)`` triées par ``path``.

    Couvrir les headers — pas seulement les entrées de fichiers — rend
    détectable AVANT tout write une altération de
    ``bank_version``/``commit_id``/``source_node_id`` : sinon un bank_version
    corrompu n'apparaîtrait qu'après écriture (cible UNSAFE) et un
    source_node_id modifié pourrait contourner le guard d'identité source.
    Reste insensible à l'ordre des entrées ; exclut ``manifest_sha256`` (le
    champ calculé) et ``created_at`` (cosmétique).
    """
    header = {
        "protocol_version": manifest.protocol_version,
        "source_node_id": manifest.source_node_id,
        "membership_epoch": manifest.membership_epoch,
        "bank_version": manifest.bank_version,
        "commit_id": manifest.commit_id,
    }
    entries = sorted(
        ((e.path, e.sha256) for e in manifest.entries), key=lambda p: p[0]
    )
    return _sha256_bytes(
        _canonical_json_bytes({"header": header, "entries": entries})
    )


# =============================================================================
# Validation complète de l'état Hivemind (export source + import cible)
# =============================================================================


async def _all_ack_event_ids(storage: StorageService, space_id: str) -> set[str]:
    """
    event_ids présents sous le préfixe ``acks/``, Y COMPRIS ceux dont l'event a
    été compacté/est absent de ``events/`` (acks orphelins). Scanner le préfixe
    directement (et non seulement les events listés) garantit qu'un ack orphelin
    corrompu ne passe pas inaperçu.
    """
    prefix = layout.ack_prefix(space_id)
    objects = await storage.list_objects(prefix)
    ids: set[str] = set()
    for obj in objects:
        rel = obj["Key"][len(prefix):]
        if "/" in rel:
            ids.add(rel.split("/", 1)[0])
    return ids


async def _validate_full_hivemind_state(
    store: HivemindStateStore, storage: StorageService, space_id: str
) -> None:
    """
    Désérialise et valide TOUT l'état Hivemind d'un space ; lève
    ``CorruptedStateError`` à la moindre invalidité (fail-closed).

    Couvre :
    - ``load_snapshot`` (singletons + queue/commits/tombstones/watermarks/
      node_status) ;
    - ``list_events`` (journal d'audit) ;
    - TOUS les acks par scan DIRECT du préfixe (y compris orphelins dont l'event
      est compacté/absent) — sinon un ack orphelin corrompu serait importé et le
      node marqué HEALTHY, puis ``list_acks`` lèverait plus tard ;
    - le parsing Ed25519 de la clé publique de CHAQUE membre ACTIVE — une clé
      non-vide mais malformée passerait l'unicité mais empoisonnerait la
      membership (``INVALID_KEY`` côté peer channel alors que les ACK de ce
      membre resteraient attendus par ``expected_ack_node_ids``).
    """
    snapshot = await store.load_snapshot()
    # Le pointeur bank_version DOIT être au niveau du dernier commit : un commit
    # ORPHELIN en avance (append_commit réussi mais pointeur resté en arrière)
    # laisserait une recovery ultérieure avancer vers une version jamais
    # committée par le pointeur. On refuse l'incohérence (export et import).
    pointer_bv = (
        snapshot.bank_version_pointer.bank_version
        if snapshot.bank_version_pointer is not None
        else -1
    )
    latest_commit_bv = max(
        (c.bank_version for c in snapshot.commits), default=-1
    )
    if latest_commit_bv > pointer_bv:
        raise CorruptedStateError(
            f"orphan commit ahead of pointer (latest commit "
            f"{latest_commit_bv} > pointer {pointer_bv})"
        )
    await store.list_events()
    for event_id in await _all_ack_event_ids(storage, space_id):
        await store.list_acks(event_id)
    membership = snapshot.membership
    if membership is not None:
        active = [
            m for m in membership.members if m.status == MemberStatus.ACTIVE.value
        ]
        if any(not m.node_id for m in active):
            raise CorruptedStateError(
                "ACTIVE member with an empty node_id in MembershipView "
                "(would wait for ACK from '' and block all-ACK)"
            )
        for member in active:
            try:
                _load_public_key(member.public_key)
            except PeerChannelError as exc:
                raise CorruptedStateError(
                    "invalid Ed25519 public key for ACTIVE member "
                    f"{member.node_id!r}: {exc}"
                ) from exc
        # Unicité des identités ACTIVE (node_id + public_key). Validée ICI (et
        # donc à l'EXPORT comme à l'import) : sinon une source aux identités
        # dupliquées (mauvais restore / set_membership manuel) produirait un
        # snapshot que l'importeur ne rejetterait qu'après avoir écrit la cible.
        node_ids = [m.node_id for m in active]
        if len(set(node_ids)) != len(node_ids):
            raise CorruptedStateError(
                "duplicate ACTIVE node_id in MembershipView"
            )
        pubkeys = [m.public_key for m in active]
        if len(set(pubkeys)) != len(pubkeys):
            raise CorruptedStateError(
                "duplicate ACTIVE public_key in MembershipView — one key "
                "would authenticate multiple nodes"
            )


# =============================================================================
# Détection d'un space Hivemind — fail-closed
# =============================================================================


@dataclass(frozen=True)
class HiveContext:
    """Contexte Hivemind résolu pour un space (read-only)."""

    space_id: str
    is_hive: bool
    node: Optional[NodeIdentity]
    membership: Optional[MembershipView]
    node_status: HiveNodeStatus


async def resolve_hive_context(
    storage: StorageService, space_id: str
) -> HiveContext:
    """
    Résout le contexte Hivemind d'un space à partir de l'état durable.

    Un space est Hivemind ssi ``node.json`` se désérialise ET ``members.json``
    contient au moins un membre ``ACTIVE``. Aucun flag dans ``_meta.json``.

    Fail-closed : la lecture passe par ``HivemindStateStore`` ; une corruption
    de ``node.json`` ou ``members.json`` lève ``CorruptedStateError`` et
    n'est PAS rattrapée ici — un space corrompu doit remonter comme
    unsafe/bloqué, jamais comme « non partagé » (sinon un write partagé
    bypasserait le token et provoquerait un split-brain).
    """
    store = HivemindStateStore(storage=storage, space_id=space_id)
    node = await store.get_node_identity()
    membership = await store.get_membership()
    health = await store.get_node_status()

    has_active = membership is not None and any(
        m.status == MemberStatus.ACTIVE.value for m in membership.members
    )
    # Un marqueur node_status explicite UNSAFE/RESYNC_REQUIRED (ex. import
    # partiel ayant posé node_status=UNSAFE avant node.json) signale un space
    # Hivemind EN COURS et non sain : il NE doit PAS être classé « local »,
    # sinon un caller bypasserait le chemin fail-closed et écrirait par-dessus
    # un demi-import. Il compte donc comme Hivemind (le guard bloquera ensuite
    # sur l'état UNSAFE/RESYNC).
    structurally_complete = node is not None and has_active
    health_status = HiveNodeStatus(health.status) if health is not None else None
    # Tout marqueur node_status (même HEALTHY) signale un space DÉJÀ Hivemind :
    # il ne doit jamais être reclassé « local ». De même, la présence de
    # node.json ou d'une membership ACTIVE.
    is_hive = node is not None or has_active or health is not None

    if not is_hive:
        node_status = HiveNodeStatus.DISABLED
    elif health_status in (HiveNodeStatus.UNSAFE, HiveNodeStatus.RESYNC_REQUIRED):
        # Marqueur explicite unsafe/resync : respecté tel quel.
        node_status = health_status
    elif structurally_complete and health_status in (None, HiveNodeStatus.HEALTHY):
        # Structure COMPLÈTE (node.json + >= 1 membre ACTIVE) et aucun marqueur
        # unsafe : sain.
        node_status = HiveNodeStatus.HEALTHY
    else:
        # is_hive mais structure INCOMPLÈTE (node.json ou membership ACTIVE
        # manquant), y compris un HEALTHY PÉRIMÉ après restore/suppression
        # partiel : fail-closed -> UNSAFE, jamais HEALTHY ni DISABLED.
        node_status = HiveNodeStatus.UNSAFE

    return HiveContext(
        space_id=space_id,
        is_hive=is_hive,
        node=node,
        membership=membership,
        node_status=node_status,
    )


async def is_hivemind_space(storage: StorageService, space_id: str) -> bool:
    """Helper booléen — voir ``resolve_hive_context`` pour le contrat
    fail-closed (la corruption propage ``CorruptedStateError``)."""
    return (await resolve_hive_context(storage, space_id)).is_hive


# =============================================================================
# Verdict de routage WriteSink — read-only, dérivé du HiveContext fail-closed
# (P3-2 / ADR-0007 ; consommé par le registre P3-7 puis #8)
# =============================================================================


class WriteRoute(str, Enum):
    """
    Décision de routage d'une mutation durable — un VALEUR, pas un sink.

    P3-2 ne construit aucun ``WriteSink`` (les classes vivent dans
    ``write_sink.py``, livré par P3-3) : il émet seulement le verdict que le
    registre P3-7 traduira en sink concret. Garder le verdict découplé des
    classes de sink évite un cycle d'import (``write_sink`` importera
    ``storage`` que ``lifecycle`` importe déjà) et garde P3-2 sans dépendance.

    ``str``-Enum pour rester aligné sur la convention du codebase
    (``HiveNodeStatus`` / ``MemberStatus`` / ``TokenState`` sont tous des
    ``str``-Enum) : sérialisable et comparable à sa chaîne.

    Trois sorties (SHARED CONTRACT routing_verdict / ADR-0007) :

    - ``DIRECT_LOCAL`` : space NON-Hivemind (``is_hive == False``) — chemin
      legacy octet-pour-octet (``DirectLocalWriteSink``). SEUL chemin vers
      direct-local.
    - ``STAGED`` : Hivemind valide et sain (``is_hive`` + ``HEALTHY``) — écriture
      partagée légitime à passer par le single-writer (``StagedHivemindWriteSink``,
      stub Wave-1 qui REFUSE bruyamment ; le vrai corps staging/commit arrive en #8).
    - ``REFUSE`` : Hivemind non sain (``UNSAFE`` / ``RESYNC_REQUIRED``) — refus
      fail-closed dur, JAMAIS dégradé en direct-local.

    ``STAGED`` et ``REFUSE`` restent DISTINCTS bien qu'en Wave-1 les deux
    finissent par refuser : ``STAGED`` porte le seam que #8 servira (écriture
    partagée valide), ``REFUSE`` est un refus non-serviçable. Les fusionner
    perdrait la distinction du contrat.
    """

    DIRECT_LOCAL = "direct_local"
    STAGED = "staged"
    REFUSE = "refuse"


def route_for_context(ctx: HiveContext) -> WriteRoute:
    """
    Mappe un ``HiveContext`` (déjà résolu fail-closed) vers un ``WriteRoute``.

    Fonction PURE et read-only (ne lit ni n'écrit aucun storage) : elle ne
    dépend que de la paire ``(ctx.is_hive, ctx.node_status)``.

    Table de routage :

    - ``is_hive == False`` (donc ``node_status == DISABLED``) -> ``DIRECT_LOCAL``.
    - ``is_hive == True`` + ``HEALTHY`` -> ``STAGED``.
    - ``is_hive == True`` + ``UNSAFE`` / ``RESYNC_REQUIRED`` -> ``REFUSE``.

    Invariant net (défense en profondeur) : SEUL ``is_hive == False`` atteint
    ``DIRECT_LOCAL``. Tout ``is_hive == True`` qui n'est pas ``HEALTHY`` —
    y compris le cas résiduel théoriquement inatteignable ``is_hive`` +
    ``DISABLED`` (que ``resolve_hive_context`` ne produit jamais : il garantit
    ``is_hive == False <=> DISABLED``) — tombe en ``REFUSE``, JAMAIS en
    ``DIRECT_LOCAL``. Un space partagé/unsafe ne doit jamais être dégradé vers
    le chemin legacy (bypass de token / split-brain).
    """
    if not ctx.is_hive:
        return WriteRoute.DIRECT_LOCAL
    if ctx.node_status == HiveNodeStatus.HEALTHY:
        return WriteRoute.STAGED
    # is_hive mais non sain (UNSAFE/RESYNC_REQUIRED) ou tout résidu défensif :
    # refus fail-closed — jamais DIRECT_LOCAL.
    return WriteRoute.REFUSE


async def resolve_write_route(
    storage: StorageService, space_id: str
) -> WriteRoute:
    """
    Résout le verdict de routage WriteSink d'un space (read-only).

    Consomme le resolver fail-closed PR#17 ``resolve_hive_context`` (il ne fait
    que des lectures via ``HivemindStateStore``) puis applique ``route_for_context``.
    Aucune écriture, aucune réparation silencieuse.

    Fail-closed — ``CorruptedStateError`` PROPAGE non rattrapée : une corruption
    de ``node.json`` / ``members.json`` / ``node_status.json`` remonte depuis le
    resolver et n'est JAMAIS convertie en ``DIRECT_LOCAL``. C'est le garde
    split-brain / bypass-de-token d'ADR-0007/ADR-0008 (SHARED CONTRACT :
    « CorruptedStateError ... is NEVER caught at the routing seam »). Le registre
    P3-7 s'appuie sur cette propagation pour échouer fermé ; un space corrompu
    n'est jamais vu comme « non partagé ».

    SEUL ``is_hive == False`` mène à ``DIRECT_LOCAL`` ; tout space
    partagé/unsafe/resync est ``STAGED`` ou ``REFUSE``, jamais direct.
    """
    ctx = await resolve_hive_context(storage, space_id)
    return route_for_context(ctx)


#: Vocabulaire de statut **produit P2** (6 valeurs), exposé PAR ``hive_status_label``
#: AU-DESSUS du resolver. ``resolve_hive_context`` reste 4-valeurs
#: (``HiveNodeStatus``) et agnostique de ``_meta.json`` ; ``hive_status()`` garde
#: sa clé 4-valeurs ``hive_status`` pour #10. Ces deux espaces de valeurs sont
#: DISTINCTS. Voir ADR-0008.
HIVE_STATUS_LABELS: tuple[str, ...] = (
    "not_a_space",
    "local_only",
    "hivemind_healthy",
    "hivemind_blocked",
    "unsafe",
    "resync_required",
)


async def hive_status_label(storage: StorageService, space_id: str) -> str:
    """
    Label de statut unifié P2 (6 valeurs) pour un space, au-dessus du resolver.

    Dérive une chaîne de la grammaire produit P2 (``HIVE_STATUS_LABELS``) à
    partir de ``resolve_hive_context`` (4 valeurs, agnostique de ``_meta.json``)
    ET de la présence de ``{space_id}/_meta.json`` :

    - ``not_a_space`` : pas Hivemind ET pas de ``_meta.json`` ;
    - ``local_only`` : pas Hivemind MAIS ``_meta.json`` présent (espace legacy) ;
    - ``hivemind_healthy`` : Hivemind, structurellement complet et sain ;
    - ``resync_required`` / ``unsafe`` : Hivemind mais santé non saine ;
    - **orphelin** : Hivemind SANS ``_meta.json`` -> ``unsafe`` (override évalué
      AVANT tout le mapping de santé — HEALTHY *et* RESYNC — pour qu'un hive
      sain ou en resync dont le ``_meta.json`` a été supprimé remonte ``unsafe``
      et jamais ``hivemind_healthy``/``resync_required``).

    ``hivemind_blocked`` n'est pas émis par l'enum 4-valeurs en V1 (il se replie
    sur ``unsafe``) ; il reste réservé pour un futur état explicitement bloqué.

    Fail-closed (porte) : la corruption de ``node.json`` / ``members.json`` /
    ``node_status.json`` lève ``CorruptedStateError`` depuis le resolver et
    **n'est PAS rattrapée ici** — un space corrompu remonte unsafe/bloqué, jamais
    ``local_only`` / ``not_a_space`` (sinon un write partagé bypasserait le
    token). Les fichiers plus profonds (term/token/bank_version) ne sont PAS lus
    par le resolver ; leur corruption est traitée au bootstrap (``load_snapshot``).

    Read-only : aucune écriture, aucune réparation silencieuse.
    """
    ctx = await resolve_hive_context(storage, space_id)
    meta_exists = await storage.exists(f"{space_id}/_meta.json")

    if not ctx.is_hive:
        return "local_only" if meta_exists else "not_a_space"

    # is_hive : space Hivemind. Override orphelin AVANT le mapping HEALTHY :
    # _hivemind/ présent mais _meta.json absent = coordination orpheline -> unsafe.
    if not meta_exists:
        return "unsafe"

    if ctx.node_status == HiveNodeStatus.HEALTHY:
        return "hivemind_healthy"
    if ctx.node_status == HiveNodeStatus.RESYNC_REQUIRED:
        return "resync_required"
    # UNSAFE (et tout autre cas non sain) -> unsafe (fail-closed).
    return "unsafe"


# =============================================================================
# Membership : add / evict / ACK-expectations (epoch monotone, explicite)
# =============================================================================


def active_members(membership: Optional[MembershipView]) -> list[Member]:
    """Membres ``ACTIVE`` uniquement — exclut ``LEAVING`` et ``EVICTED``."""
    if membership is None:
        return []
    return [m for m in membership.members if m.status == MemberStatus.ACTIVE.value]


def expected_ack_node_ids(membership: Optional[MembershipView]) -> list[str]:
    """node_ids dont un ACK est attendu (all-ACK conservateur, HIVEMIND.md
    §6.1) : les membres ``ACTIVE``. Un peer évincé/partant en est exclu."""
    return [m.node_id for m in active_members(membership)]


# Verrous de membership PAR space (mono-processus V1). ``set_membership`` est un
# read-modify-write sans CAS et ne rejette que les epochs inférieurs : deux
# mutations concurrentes partant de la même vue calculeraient le même
# ``epoch+1`` et la 2ᵉ écriture écraserait la 1ʳᵉ (lost update). On sérialise
# donc les mutations d'un même space. La vraie sérialisation distribuée
# (#6/#7) viendra par la queue + le token ; ici on protège le plan admin local.
# space_id -> (event_loop, lock). On lie le verrou à la boucle courante et on
# le recrée si la boucle a changé : un asyncio.Lock est attaché à sa boucle, et
# le réutiliser depuis une autre boucle lèverait. En production (boucle unique
# longue) c'est un verrou par-space stable ; en test (une boucle par test) il
# est recréé proprement.
_MEMBERSHIP_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _membership_lock(space_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _MEMBERSHIP_LOCKS.get(space_id)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _MEMBERSHIP_LOCKS[space_id] = (loop, lock)
        return lock
    return entry[1]


class MembershipService:
    """
    Mutations **explicites** de la membership partagée.

    SEUL endroit qui bumpe ``epoch`` : ``add_member`` (+1) et
    ``evict_member`` (+1, sur confirmation opérateur). Le bump d'epoch est ce
    qui fence les messages des peers restés sur l'ancien epoch via
    ``HivemindPeerChannel._verify`` (WRONG_MEMBERSHIP_EPOCH).
    """

    def __init__(self, store: HivemindStateStore) -> None:
        self._store = store

    def _space_lock(self) -> asyncio.Lock:
        return _membership_lock(self._store.space_id)

    def space_lock(self) -> asyncio.Lock:
        """The process-global per-space membership lock. A caller may hold it to
        make a multi-step check-then-mutate (e.g. Mesh approve's epoch check +
        admit + bootstrap export) atomic against every membership mutation
        (admit/promote/evict/re-scope), then invoke the ``*_locked`` variants."""
        return self._space_lock()

    async def _current_view(self) -> MembershipView:
        # Muter la membership exige un contexte Hivemind STRUCTURELLEMENT COMPLET
        # et SAIN : node.json présent + >= 1 membre ACTIVE + node_status absent
        # ou HEALTHY. Un état partiel (node.json manquant), un HEALTHY PÉRIMÉ
        # après restore partiel, ou UNSAFE/RESYNC_REQUIRED -> refus fail-closed.
        # Sinon un node corrompu/en retard écraserait la composition partagée
        # (et appenderait des events d'origine 'unknown' faute de node.json).
        node = await self._store.get_node_identity()
        view = await self._store.get_membership()
        health = await self._store.get_node_status()
        has_active = view is not None and any(
            m.status == MemberStatus.ACTIVE.value for m in view.members
        )
        health_status = HiveNodeStatus(health.status) if health is not None else None
        if (
            node is None
            or not has_active
            or health_status not in (None, HiveNodeStatus.HEALTHY)
        ):
            raise BootstrapError(
                "membership mutation refused: incomplete or unhealthy Hivemind "
                f"context (node.json={'present' if node else 'absent'}, "
                f"active_member={has_active}, node_status={health_status}) — "
                "initialization/resync required before any mutation"
            )
        assert view is not None  # garanti par has_active
        return view

    def _membership_event_id(
        self, event_type: EventType, node_id: str, epoch: int
    ) -> str:
        """event_id DÉTERMINISTE pour une transition de membership : un retry de
        la même transition (même type/node/epoch) produit le MÊME id, donc
        ``append_event`` déduplique. C'est ce qui rend l'écriture event+membership
        recouvrable."""
        return uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"{self._store.space_id}:{event_type.value}:{node_id}:{epoch}",
        ).hex

    async def _append_event(
        self,
        event_type: EventType,
        epoch: int,
        payload: dict[str, Any],
        *,
        event_id: Optional[str] = None,
    ) -> None:
        node = await self._store.get_node_identity()
        origin = node.node_id if node is not None else "unknown"
        term_state = await self._store.get_term()
        term = term_state.term if term_state is not None else 0
        await self._store.append_event(
            EventEnvelope(
                event_id=event_id or uuid.uuid4().hex,
                type=event_type,
                origin_node_id=origin,
                term=term,
                membership_epoch=epoch,
                payload=payload,
            )
        )

    async def add_member(self, member: Member) -> MembershipView:
        """
        Ajoute un peer via une opération explicite (jamais de join implicite
        depuis un message reçu). Refuse un node_id déjà ACTIVE.

        Bumpe ``epoch`` de +1, persiste, puis appose un event ``PEER_JOINED``.

        Sérialisé par un verrou par-space (read-modify-write sans CAS) : sinon
        deux mutations concurrentes écraseraient mutuellement la membership.
        """
        async with self._space_lock():
            return await self._add_member_locked(member)

    async def _add_member_locked(self, member: Member) -> MembershipView:
        if not member.node_id:
            raise BootstrapError(
                "add_member refused: empty node_id — invalid member identity "
                "(would create an ACK wait for '' and an unusable peer)"
            )
        # Fence: refuse an epoch-advancing operator mutation while a Mesh pairing is
        # mid-activation (promotion -> confirmed), so it cannot split source/target
        # epochs (no-op when Mesh is disabled).
        await assert_no_pairing_activation(self._store.space_id)
        view = await self._current_view()
        if any(
            m.node_id == member.node_id and m.status == MemberStatus.ACTIVE.value
            for m in view.members
        ):
            raise BootstrapError(
                f"node_id {member.node_id!r} is already an ACTIVE member"
            )
        if not member.public_key:
            raise BootstrapError(
                f"add_member requires a public_key for {member.node_id!r} "
                "(auth pair #4)"
            )
        # Unicité de la clé publique parmi les membres ACTIVE : une même clé
        # privée ne doit pas pouvoir s'authentifier comme plusieurs nodes, et
        # le lookup d'identité PAR clé publique au bootstrap (import_snapshot)
        # serait sinon ambigu.
        if any(
            m.public_key == member.public_key
            and m.status == MemberStatus.ACTIVE.value
            and m.node_id != member.node_id
            for m in view.members
        ):
            raise BootstrapError(
                "public_key is already used by another ACTIVE member — "
                "ambiguous identity"
            )
        # La public_key doit parser comme une vraie clé Ed25519 AVANT d'écrire :
        # une clé non-vide mais malformée serait admise ACTIVE, comptée dans
        # expected_ack_node_ids, mais le peer channel échouerait INVALID_KEY —
        # le cluster attendrait alors indéfiniment un participant inutilisable.
        try:
            _load_public_key(member.public_key)
        except PeerChannelError as exc:
            raise BootstrapError(
                f"add_member refused: non-Ed25519 public_key for "
                f"{member.node_id!r} ({exc})"
            ) from exc

        # Conserver les membres non-actifs (audit) et remplacer une entrée
        # existante non-active du même node_id le cas échéant.
        next_members = [m for m in view.members if m.node_id != member.node_id]
        active_member = member.model_copy(update={"status": MemberStatus.ACTIVE.value})
        next_members.append(active_member)

        new_epoch = view.epoch + 1
        new_view = MembershipView(epoch=new_epoch, members=next_members)
        # Event AVANT membership, avec un event_id DÉTERMINISTE : rend
        # l'opération recouvrable. Si set_membership échoue après l'event, un
        # retry recalcule le même new_epoch (vue inchangée) -> même event_id
        # (dedup no-op) -> set_membership applique. L'event ne peut jamais
        # manquer alors que la membership est commitée.
        await self._append_event(
            EventType.PEER_JOINED,
            new_epoch,
            {"node_id": member.node_id, "epoch": new_epoch},
            event_id=self._membership_event_id(
                EventType.PEER_JOINED, member.node_id, new_epoch
            ),
        )
        return await self._store.set_membership(new_view)

    async def update_member_scopes(
        self, node_id: str, scopes: Optional[list[str]]
    ) -> MembershipView:
        """
        Re-scope un membre déjà ACTIVE (ADR-0016).

        Émet ``MEMBERSHIP_UPDATED`` — l'événement de mutation de membership
        GÉNÉRIQUE (ni join ni eviction) ; un re-scoping EST une telle mutation
        et doit fencer les peers restés sur l'ancien epoch (un peer narrowed ne
        doit plus voir ses anciens droits acceptés). Bumpe ``epoch`` de +1 via
        la même machinerie événement-avant-membership que ``add_member``
        (ADR-0016 : la réconciliation d'enrollment émet ``MEMBERSHIP_UPDATED`` ;
        join/revoke gardent les types spécifiques ``PEER_JOINED``/
        ``PEER_EVICTED``). No-op (aucun bump) si les scopes sont identiques.

        Sérialisé par le verrou par-space comme ``add_member`` (read-modify-write
        sans CAS). ``scopes`` est le jeu DÉJÀ normalisé (``None`` = full).
        """
        async with self._space_lock():
            return await self._update_member_scopes_locked(node_id, scopes)

    async def _update_member_scopes_locked(
        self, node_id: str, scopes: Optional[list[str]]
    ) -> MembershipView:
        """Corps de ``update_member_scopes`` SANS acquérir le verrou.

        Présuppose que l'appelant détient déjà ``_space_lock()``. La réconciliation
        multi-deltas (``EnrollmentService.reconcile``) passe elle par
        ``apply_membership_plan`` sous le même verrou — les deux chemins
        époch-avançants consultent la fence d'activation de pairing."""
        # Fence: refuse a re-scope while a Mesh pairing is mid-activation
        # (promotion -> confirmed), so it cannot advance the source epoch past e+2
        # and split source/target MembershipViews (no-op when Mesh is disabled).
        await assert_no_pairing_activation(self._store.space_id)
        view = await self._current_view()
        target = next(
            (m for m in view.members if m.node_id == node_id), None
        )
        if target is None or target.status != MemberStatus.ACTIVE.value:
            raise BootstrapError(
                f"update_member_scopes: {node_id!r} is absent or not ACTIVE"
            )
        if target.scopes == scopes:  # idempotent : aucun bump.
            return view
        # VALIDER/normaliser les scopes AVANT tout write (event puis
        # membership). ``model_copy(update={"scopes": ...})`` N'EXÉCUTE PAS
        # les field_validators de pydantic (même sous
        # ``validate_assignment=True``) : un scope hors vocabulaire fermé
        # (ex. ["admin"]) passerait silencieusement et corromprait
        # members.json — la lecture suivante échouerait alors en
        # CorruptedStateError, mais c'est CE helper qui aurait créé la
        # corruption critique. On assigne donc ``scopes`` via le validator
        # pydantic (``validate_assignment``) sur une COPIE : il RÉ-EXÉCUTE
        # ``Member._validate_scopes`` (normalise/tri/déduplique, None=full)
        # et lève ICI sur un scope invalide — avant ``_append_event`` et
        # avant ``set_membership``. Une copie préserve à l'octet tous les
        # autres champs du membre cible.
        rescoped_target = target.model_copy(deep=True)
        Member.__pydantic_validator__.validate_assignment(
            rescoped_target, "scopes", scopes
        )
        next_members = [
            rescoped_target if m.node_id == node_id else m
            for m in view.members
        ]
        new_epoch = view.epoch + 1
        # Event AVANT membership (event_id déterministe) : recouvrable comme
        # add_member. ``rescoped: True`` distingue cette transition dans le
        # journal d'audit.
        await self._append_event(
            EventType.MEMBERSHIP_UPDATED,
            new_epoch,
            {"node_id": node_id, "epoch": new_epoch, "rescoped": True},
            event_id=self._membership_event_id(
                EventType.MEMBERSHIP_UPDATED, node_id, new_epoch
            ),
        )
        return await self._store.set_membership(
            MembershipView(epoch=new_epoch, members=next_members)
        )

    async def evict_member(
        self,
        node_id: str,
        *,
        operator: str,
        confirm: bool = False,
        reason: str = "",
        expected_incarnation: Optional[str] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        """
        Évince un peer — exige une confirmation opérateur explicite
        (HIVEMIND.md §6.1). Refuse si ``confirm`` est faux ou ``operator``
        vide.

        Le membre passe ``EVICTED`` (record conservé pour audit, jamais
        supprimé), ``epoch`` est bumpé de +1, et un event ``PEER_EVICTED``
        enregistre opérateur + raison + confirmation. L'évincé sort des
        exigences all-ACK futures (``active_members`` l'exclut).

        ``expected_incarnation`` (P10-3) : si fourni, l'éviction est un
        compare-and-evict ATOMIQUE sous le lock membership — elle échoue closed
        (``MembershipIncarnationError``) si le membre courant ne porte pas cette
        incarnation, empêchant un appariement retenu d'évincer un ré-enrôlement
        (incarnation différente) de la même identité.
        """
        if not confirm:
            raise BootstrapError(
                "eviction refused: explicit operator confirmation required "
                "(confirm=True)"
            )
        if not operator:
            raise BootstrapError("Eviction refused: operator identity is required")

        async with self._space_lock():
            return await self._evict_member_locked(
                node_id, operator=operator, reason=reason,
                expected_incarnation=expected_incarnation,
                activation_pair_id=activation_pair_id,
            )

    async def _evict_member_locked(
        self, node_id: str, *, operator: str, reason: str,
        expected_incarnation: Optional[str] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        view = await self._current_view()
        target = next((m for m in view.members if m.node_id == node_id), None)
        if target is None:
            raise BootstrapError(
                f"eviction impossible: node_id {node_id!r} is absent from membership"
            )
        # Compare-and-evict incarnation gate (atomic under the space lock): the
        # target the caller intends to evict must still be the exact incarnation it
        # named. A re-admission of the same identity carries a fresh incarnation, so
        # this fails closed rather than evicting the newer live member. ``None`` is
        # not an exception when the caller supplied an expected value: bootstrap
        # export intentionally strips source-local tags on peers, and accepting a
        # missing tag would let a retained stale pairing evict a re-enrolled member.
        if (
            expected_incarnation is not None
            and target.incarnation != expected_incarnation
        ):
            raise MembershipIncarnationError(
                "eviction refused: stale incarnation — node was re-enrolled "
                "since this pairing"
            )

        # Idempotence : un node déjà non-ACTIVE (EVICTED/LEAVING) n'est PAS
        # ré-évincé. Sinon un retry opérateur re-bumperait l'epoch et fencerait
        # des messages valides en vol / forcerait un resync alors que la
        # composition n'a pas changé. No-op : on retourne la vue inchangée.
        if target.status != MemberStatus.ACTIVE.value:
            return view

        # Refuser l'éviction du DERNIER membre ACTIVE : une membership à zéro
        # actif ferait voir le space comme non-Hivemind (is_hive = node.json +
        # >= 1 membre ACTIVE), bypassant le chemin de sûreté partagé alors que
        # l'état _hivemind existe toujours. Le teardown explicite est hors V1.
        remaining_active = [
            m
            for m in view.members
            if m.status == MemberStatus.ACTIVE.value and m.node_id != node_id
        ]
        if target.status == MemberStatus.ACTIVE.value and not remaining_active:
            raise BootstrapError(
                "eviction refused: last ACTIVE member — a space without active "
                "members would appear non-Hivemind (explicit teardown is outside V1)"
            )

        # Fence: this is an epoch-advancing eviction. Refuse it while a DIFFERENT
        # Mesh pairing is mid-activation (``activation_pair_id`` is the caller's OWN
        # pairing, exempt so its give-up can converge). No-op when Mesh is disabled.
        await assert_no_pairing_activation(
            self._store.space_id, ignore_pair_id=activation_pair_id
        )

        next_members = [
            (
                m.model_copy(update={"status": MemberStatus.EVICTED.value})
                if m.node_id == node_id
                else m
            )
            for m in view.members
        ]
        new_epoch = view.epoch + 1
        new_view = MembershipView(epoch=new_epoch, members=next_members)
        # Event AVANT membership (event_id déterministe) : recouvrable comme
        # add_member — un retry après échec de set_membership déduplique l'event
        # et applique la membership.
        await self._append_event(
            EventType.PEER_EVICTED,
            new_epoch,
            {
                "node_id": node_id,
                "epoch": new_epoch,
                "operator": operator,
                "reason": reason,
                "confirmed": True,
            },
            event_id=self._membership_event_id(
                EventType.PEER_EVICTED, node_id, new_epoch
            ),
        )
        return await self._store.set_membership(new_view)

    # ------------------------------------------------------------------
    # Project Mesh two-epoch enrolment primitives (P10-3, ADR-0024).
    #
    # ``add_member`` force ACTIVE, ce qui collapserait les deux transitions
    # fencées (admit PENDING e->e+1, puis activate PENDING->ACTIVE e+1->e+2) en
    # une seule. Ces primitives préservent le statut PENDING et réutilisent
    # VERBATIM la discipline event-avant-membership + event_id déterministe +
    # gate ``_current_view`` (sauf ``apply_self_activation`` — voir sa docstring).
    # ------------------------------------------------------------------

    async def admit_pending_candidate(
        self,
        candidate: Member,
        *,
        expected_epoch: Optional[int] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        """Admet un candidat Mesh en statut PENDING (Transition 1, e -> e+1).

        Émet ``MEMBERSHIP_UPDATED`` (le candidat n'est ni un join ACTIVE ni une
        éviction). Unicité node_id ET clé publique imposée sur ACTIVE ∪ PENDING.
        Health-gated (source saine) comme ``add_member``. ``expected_epoch`` :
        compare-and-admit atomique (voir ``admit_pending_candidate_locked``).
        """
        async with self._space_lock():
            return await self.admit_pending_candidate_locked(
                candidate,
                expected_epoch=expected_epoch,
                activation_pair_id=activation_pair_id,
            )

    async def admit_pending_candidate_locked(
        self,
        candidate: Member,
        *,
        expected_epoch: Optional[int] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        """Public locked variant — the caller MUST already hold :meth:`space_lock`.
        Compare-and-admit: if ``expected_epoch`` is given, admits only when the
        current epoch matches (else ``MembershipEpochError``), so a concurrent
        mutation cannot advance the epoch between an out-of-lock check and this
        admission."""

        return await self._admit_pending_candidate_locked(
            candidate,
            expected_epoch=expected_epoch,
            activation_pair_id=activation_pair_id,
        )

    async def _admit_pending_candidate_locked(
        self,
        candidate: Member,
        *,
        expected_epoch: Optional[int] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        if not candidate.node_id:
            raise BootstrapError("admit_pending refused: node_id is empty")
        if not candidate.public_key:
            raise BootstrapError(
                f"admit_pending requires a public_key for {candidate.node_id!r}"
            )
        view = await self._current_view()
        if expected_epoch is not None and view.epoch != expected_epoch:
            raise MembershipEpochError(
                f"admit_pending refused: expected epoch {expected_epoch}, "
                f"current {view.epoch} (concurrent mutation)"
            )
        occupied = (MemberStatus.ACTIVE.value, MemberStatus.PENDING.value)
        if any(
            m.node_id == candidate.node_id and m.status in occupied
            for m in view.members
        ):
            raise BootstrapError(
                f"node_id {candidate.node_id!r} is already an ACTIVE or PENDING member"
            )
        if any(
            m.public_key == candidate.public_key
            and m.status in occupied
            and m.node_id != candidate.node_id
            for m in view.members
        ):
            raise BootstrapError(
                "public_key is already used by another ACTIVE/PENDING member — "
                "ambiguous identity"
            )
        try:
            _load_public_key(candidate.public_key)
        except PeerChannelError as exc:
            raise BootstrapError(
                f"admit_pending refused: non-Ed25519 public_key for "
                f"{candidate.node_id!r} ({exc})"
            ) from exc
        # Fence: this admission bumps the epoch. Refuse it while a DIFFERENT Mesh
        # pairing is mid-activation (its precomputed e+2 would be split by the
        # bump); the caller's OWN pairing (``activation_pair_id``) is exempt. This
        # pairing's own session is 'transferring' here (not a mid-activation state),
        # so the bypass is belt-and-suspenders. No-op when Mesh is disabled.
        await assert_no_pairing_activation(
            self._store.space_id, ignore_pair_id=activation_pair_id
        )
        next_members = [m for m in view.members if m.node_id != candidate.node_id]
        pending_member = candidate.model_copy(
            update={"status": MemberStatus.PENDING.value}
        )
        next_members.append(pending_member)
        new_epoch = view.epoch + 1
        new_view = MembershipView(epoch=new_epoch, members=next_members)
        await self._append_event(
            EventType.MEMBERSHIP_UPDATED,
            new_epoch,
            {
                "node_id": candidate.node_id,
                "epoch": new_epoch,
                "status": MemberStatus.PENDING.value,
            },
            event_id=self._membership_event_id(
                EventType.MEMBERSHIP_UPDATED, candidate.node_id, new_epoch
            ),
        )
        return await self._store.set_membership(new_view)

    async def promote_pending_to_active(
        self,
        node_id: str,
        *,
        expected_epoch: Optional[int] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        """Promeut un candidat PENDING -> ACTIVE (Transition 2, e+1 -> e+2).

        Utilisé CÔTÉ SOURCE après le full-mesh all-ACK sur le digest de vue
        candidate e+2. Health-gated. Idempotent : un node déjà ACTIVE -> no-op.
        Refuse tout node non-PENDING (jamais de promotion depuis absent/evicted).
        ``expected_epoch`` : compare-and-promote atomique — ne promeut que si
        l'epoch courant correspond (sinon ``MembershipEpochError``), empêchant une
        mutation concurrente d'avancer l'epoch entre le contrôle hors-lock et la
        promotion (l'idempotent déjà-ACTIVE reste un no-op quel que soit l'epoch).
        """
        async with self._space_lock():
            return await self._promote_pending_to_active_locked(
                node_id,
                expected_epoch=expected_epoch,
                activation_pair_id=activation_pair_id,
            )

    async def promote_pending_to_active_locked(
        self,
        node_id: str,
        *,
        expected_epoch: Optional[int] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        """Locked counterpart to :meth:`promote_pending_to_active`.

        Callers that need to make a protocol-state revalidation and Transition 2
        one local critical section MUST already hold :meth:`space_lock`.  Keeping
        this public, symmetric with ``admit_pending_candidate_locked``, avoids a
        non-reentrant lock reacquisition between the check and promotion.
        """

        return await self._promote_pending_to_active_locked(
            node_id,
            expected_epoch=expected_epoch,
            activation_pair_id=activation_pair_id,
        )

    async def _promote_pending_to_active_locked(
        self,
        node_id: str,
        *,
        expected_epoch: Optional[int] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        view = await self._current_view()
        target = next((m for m in view.members if m.node_id == node_id), None)
        if target is None:
            raise BootstrapError(
                f"promotion impossible: node_id {node_id!r} is absent from membership"
            )
        if target.status == MemberStatus.ACTIVE.value:
            return view  # idempotent no-op (l'epoch n'est pas contrôlé ici)
        if expected_epoch is not None and view.epoch != expected_epoch:
            raise MembershipEpochError(
                f"promotion refused: expected epoch {expected_epoch}, "
                f"current {view.epoch} (concurrent mutation)"
            )
        if target.status != MemberStatus.PENDING.value:
            raise BootstrapError(
                f"promotion refused: node_id {node_id!r} is not PENDING "
                f"(status={target.status})"
            )
        # Fence: this IS Transition 2 (the e+1 -> e+2 promotion). The pairing that
        # owns it passes its ``activation_pair_id`` so it is not blocked by its own
        # awaiting_acks session; a DIFFERENT pairing mid-activation still refuses
        # (its precomputed e+2 would be split). No-op when Mesh is disabled.
        await assert_no_pairing_activation(
            self._store.space_id, ignore_pair_id=activation_pair_id
        )
        next_members = [
            (
                m.model_copy(update={"status": MemberStatus.ACTIVE.value})
                if m.node_id == node_id
                else m
            )
            for m in view.members
        ]
        new_epoch = view.epoch + 1
        new_view = MembershipView(epoch=new_epoch, members=next_members)
        await self._append_event(
            EventType.MEMBERSHIP_UPDATED,
            new_epoch,
            {
                "node_id": node_id,
                "epoch": new_epoch,
                "status": MemberStatus.ACTIVE.value,
            },
            event_id=self._membership_event_id(
                EventType.MEMBERSHIP_UPDATED, node_id, new_epoch
            ),
        )
        return await self._store.set_membership(new_view)

    async def remove_pending_candidate(
        self,
        node_id: str,
        *,
        operator: str,
        reason: str = "",
        confirm: bool = False,
        expected_incarnation: Optional[str] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        """Retire un candidat PENDING via l'autorité membership (PEER_EVICTED, +1).

        ``evict_member`` no-op sur tout non-ACTIVE ; ceci gère explicitement le
        retrait d'un candidat PENDING abandonné/évincé (PROJECT_MESH.md:247) — il
        sort du candidate set par un bump d'epoch. Ne retire QUE des PENDING.
        """
        if not confirm:
            raise BootstrapError("Candidate removal refused: confirmation is required")
        if not operator:
            raise BootstrapError("Candidate removal refused: operator identity is required")
        async with self._space_lock():
            return await self._remove_pending_candidate_locked(
                node_id,
                operator=operator,
                reason=reason,
                expected_incarnation=expected_incarnation,
                activation_pair_id=activation_pair_id,
            )

    async def _remove_pending_candidate_locked(
        self,
        node_id: str,
        *,
        operator: str,
        reason: str,
        expected_incarnation: Optional[str] = None,
        activation_pair_id: Optional[str] = None,
    ) -> MembershipView:
        view = await self._current_view()
        target = next((m for m in view.members if m.node_id == node_id), None)
        if target is None:
            raise BootstrapError(
                f"candidate removal impossible: node_id {node_id!r} is absent from membership"
            )
        # The source pairing's give-up is a compare-and-remove operation, just
        # like force eviction is compare-and-evict.  Without this exact check a
        # valid-schema rewrite of a PENDING member's incarnation could make a
        # retained pairing remove a different candidate under the same node id.
        if (
            expected_incarnation is not None
            and target.incarnation != expected_incarnation
        ):
            raise MembershipIncarnationError(
                "candidate removal refused: stale incarnation — node was re-enrolled "
                "since this pairing"
            )
        # Ne retire QUE des PENDING ; un ACTIVE passe par evict_member (garde
        # dernier-actif). Un déjà-non-PENDING est idempotent no-op.
        if target.status != MemberStatus.PENDING.value:
            return view
        # Fence: this removal bumps the epoch. Refuse it while a DIFFERENT Mesh
        # pairing is mid-activation; the caller's OWN pairing (``activation_pair_
        # id``) is exempt so its give-up (evict) can converge. No-op sans Mesh.
        await assert_no_pairing_activation(
            self._store.space_id, ignore_pair_id=activation_pair_id
        )
        next_members = [
            (
                m.model_copy(update={"status": MemberStatus.EVICTED.value})
                if m.node_id == node_id
                else m
            )
            for m in view.members
        ]
        new_epoch = view.epoch + 1
        new_view = MembershipView(epoch=new_epoch, members=next_members)
        await self._append_event(
            EventType.PEER_EVICTED,
            new_epoch,
            {
                "node_id": node_id,
                "epoch": new_epoch,
                "operator": operator,
                "reason": reason,
                "confirmed": True,
            },
            event_id=self._membership_event_id(
                EventType.PEER_EVICTED, node_id, new_epoch
            ),
        )
        return await self._store.set_membership(new_view)

    async def apply_self_activation(self, *, expected_epoch: int) -> MembershipView:
        """Le node LOCAL se promeut PENDING -> ACTIVE (Transition 2, côté CIBLE).

        NON health-gated à dessein : la cible d'un enrôlement Mesh est
        délibérément ``node_status`` UNSAFE tant qu'elle est PENDING, et c'est
        CETTE transition (suivie du flip HEALTHY par l'appelant) qui la rend
        saine. Confinement fort : ne promeut QUE le node local (``node.json``),
        et seulement s'il est PENDING à ``expected_epoch`` exact — il ne peut
        jamais promouvoir un pair, ni promouvoir depuis un autre statut/epoch.

        L'appelant (``mesh.membership_sync.apply_pending_self_activation``) DOIT
        avoir vérifié, AVANT, l'éligibilité de la source signataire et l'égalité
        du digest de vue candidate e+2 signé par la source. Idempotent : déjà à
        ``expected_epoch+1`` avec self ACTIVE -> no-op.
        """
        async with self._space_lock():
            return await self.apply_self_activation_locked(
                expected_epoch=expected_epoch
            )

    async def apply_self_activation_locked(
        self, *, expected_epoch: int
    ) -> MembershipView:
        """Locked counterpart to :meth:`apply_self_activation`.

        Mesh activation can validate an import authority and promote the local
        PENDING member only while the caller holds :meth:`space_lock`; exposing
        this avoids a non-reentrant lock gap between those two operations.
        """

        node = await self._store.get_node_identity()
        view = await self._store.get_membership()
        if node is None or view is None:
            raise BootstrapError(
                "self-activation refused: incomplete local state (node/membership)"
            )
        self_id = node.node_id
        target = next((m for m in view.members if m.node_id == self_id), None)
        if target is None:
            raise BootstrapError(
                "self-activation refused: local node is absent from membership"
            )
        new_epoch = expected_epoch + 1
        if view.epoch == new_epoch and target.status == MemberStatus.ACTIVE.value:
            return view  # idempotent : déjà appliqué
        if view.epoch != expected_epoch:
            raise BootstrapError(
                f"self-activation refused: local epoch {view.epoch} != "
                f"expected {expected_epoch}"
            )
        if target.status != MemberStatus.PENDING.value:
            raise BootstrapError(
                f"self-activation refused: local node is not PENDING "
                f"(status={target.status})"
            )
        next_members = [
            (
                m.model_copy(update={"status": MemberStatus.ACTIVE.value})
                if m.node_id == self_id
                else m
            )
            for m in view.members
        ]
        new_view = MembershipView(epoch=new_epoch, members=next_members)
        await self._append_event(
            EventType.MEMBERSHIP_UPDATED,
            new_epoch,
            {
                "node_id": self_id,
                "epoch": new_epoch,
                "status": MemberStatus.ACTIVE.value,
            },
            event_id=self._membership_event_id(
                EventType.MEMBERSHIP_UPDATED, self_id, new_epoch
            ),
        )
        return await self._store.set_membership(new_view)

    async def apply_membership_plan(
        self,
        *,
        add: list[Member],
        rescope: list[tuple[str, Optional[list[str]]]],
        revoke: list[str],
        operator: str,
        reason: str,
        base_view: MembershipView,
    ) -> MembershipView:
        """Applique un plan multi-deltas (revoke/add/rescope) en UNE SEULE écriture.

        Atomicité tout-ou-rien (ADR-0008) : la vue cible est construite ENTIÈREMENT
        EN MÉMOIRE et validée contre TOUS les invariants membership AVANT le
        moindre write. Si un seul invariant casse, ``BootstrapError`` est levée et
        AUCUNE écriture n'a eu lieu — la membership reste exactement ``base_view``.
        Puis : un event par delta (event_id DÉTERMINISTE, comme les primitifs
        unitaires) au même ``new_epoch = base_view.epoch + 1``, suivi d'UNE seule
        ``set_membership``. Un seul bump d'epoch, un seul members.json écrit.

        PRÉSUPPOSE le verrou ``_space_lock()`` déjà détenu par l'appelant
        (``EnrollmentService.reconcile``). Re-lit la vue VERROUILLÉE via
        ``_current_view`` (gate node.json + actif + santé) et REFUSE fail-closed si
        son epoch diverge de ``base_view`` (le plan a été calculé sur une vue
        périmée — une mutation concurrente s'est glissée avant l'acquisition du
        verrou côté appelant).

        Construit chaque membre EXACTEMENT comme les primitifs unitaires
        (``add_member`` / ``update_member_scopes`` / ``evict_member``) pour garantir
        un members.json OCTET-POUR-OCTET identique au chemin séquentiel.
        """
        # Fence: comme ``add_member`` / ``update_member_scopes``, une réconciliation
        # multi-deltas fait AVANCER l'epoch ; la refuser tant qu'un pairing Mesh est
        # en cours d'activation (promotion -> confirmée) empêche le même split
        # source/cible (no-op quand Mesh est désactivé). Sous le ``_space_lock()``
        # déjà détenu par l'appelant.
        await assert_no_pairing_activation(self._store.space_id)
        view = await self._current_view()
        # Détection de plan périmé : la vue verrouillée doit être CELLE sur
        # laquelle le plan a été calculé. Toute divergence = mutation concurrente
        # intercalée -> fail-closed sans rien appliquer (jamais un plan périmé).
        if view.epoch != base_view.epoch:
            raise BootstrapError(
                "stale enrollment plan: membership changed "
                f"(expected epoch {base_view.epoch}, locked view {view.epoch}) "
                "— reconciliation refused; no mutation"
            )

        revoke_set = set(revoke)
        # Index ACTIVE courant pour valider revoke/rescope et la collision de clés.
        active_by_id = {
            m.node_id: m
            for m in view.members
            if m.status == MemberStatus.ACTIVE.value
        }

        # --- Validation REVOKE : chaque cible doit être un membre ACTIVE. ----
        for node_id in revoke:
            target = active_by_id.get(node_id)
            if target is None:
                raise BootstrapError(
                    f"revocation impossible: {node_id!r} is absent or not ACTIVE"
                )

        # --- Validation ADD : node_id pas déjà ACTIVE, clé valide & unique. --
        added_ids = {m.node_id for m in add}
        # Clés publiques qui SURVIVRONT (membres ACTIVE non révoqués).
        surviving_keys: dict[str, str] = {
            m.public_key: nid
            for nid, m in active_by_id.items()
            if nid not in revoke_set and m.public_key
        }
        for member in add:
            if not member.node_id:
                raise BootstrapError(
                    "add_member refused: empty node_id — invalid identity"
                )
            if (
                member.node_id in active_by_id
                and member.node_id not in revoke_set
            ):
                raise BootstrapError(
                    f"node_id {member.node_id!r} is already an ACTIVE member"
                )
            if not member.public_key:
                raise BootstrapError(
                    f"add_member requires a public_key for {member.node_id!r}"
                )
            try:
                _load_public_key(member.public_key)
            except PeerChannelError as exc:
                raise BootstrapError(
                    f"add_member refused: non-Ed25519 public_key for "
                    f"{member.node_id!r} ({exc})"
                ) from exc
            owner = surviving_keys.get(member.public_key)
            if owner is not None and owner != member.node_id:
                raise BootstrapError(
                    "public_key is already used by another ACTIVE member — "
                    "ambiguous identity"
                )
            surviving_keys[member.public_key] = member.node_id

        # --- Validation RESCOPE : cible ACTIVE, scopes valides (validator). --
        rescope_targets: dict[str, Member] = {}
        for node_id, scopes in rescope:
            target = active_by_id.get(node_id)
            if target is None:
                raise BootstrapError(
                    f"update_member_scopes: {node_id!r} is absent or not ACTIVE"
                )
            # Valide/normalise via le validator pydantic sur une COPIE (jamais
            # ``model_copy(update=...)`` qui shunte les field_validators) : un
            # scope hors vocabulaire fermé lève ICI, avant tout write.
            rescoped_target = target.model_copy(deep=True)
            Member.__pydantic_validator__.validate_assignment(
                rescoped_target, "scopes", scopes
            )
            rescope_targets[node_id] = rescoped_target

        # --- Garde « dernier membre ACTIVE » sur la vue CIBLE complète. ------
        remaining_active = (set(active_by_id) - revoke_set) | added_ids
        if not remaining_active:
            raise BootstrapError(
                "application refused: plan would remove the last ACTIVE member "
                "— a space without active members would appear non-Hivemind"
            )

        # --- Construction de la vue cible EN MÉMOIRE (aucun write encore). ---
        # 1) revoke : passe le membre existant EVICTED (à sa place, copie).
        # 2) rescope : remplace par la copie re-scopée validée (à sa place).
        # 3) add : remplace toute entrée non-active du même node_id, sinon append
        #    en queue — IDENTIQUE à ``_add_member_locked``.
        next_members: list[Member] = []
        for m in view.members:
            if m.node_id in revoke_set and m.status == MemberStatus.ACTIVE.value:
                next_members.append(
                    m.model_copy(update={"status": MemberStatus.EVICTED.value})
                )
            elif m.node_id in rescope_targets:
                next_members.append(rescope_targets[m.node_id])
            elif m.node_id in added_ids:
                # Entrée non-active préexistante du même node_id : retirée ici,
                # ré-ajoutée ACTIVE plus bas (ordre = queue, comme le primitif).
                continue
            else:
                next_members.append(m)
        for member in add:
            next_members.append(
                member.model_copy(update={"status": MemberStatus.ACTIVE.value})
            )

        new_epoch = view.epoch + 1
        new_view = MembershipView(epoch=new_epoch, members=next_members)

        # --- Events AVANT membership (event_id DÉTERMINISTE, recouvrable). ---
        # Ordre d'audit : revoke (PEER_EVICTED), add (PEER_JOINED), rescope
        # (MEMBERSHIP_UPDATED) — chaque type identique au primitif unitaire.
        for node_id in revoke:
            await self._append_event(
                EventType.PEER_EVICTED,
                new_epoch,
                {
                    "node_id": node_id,
                    "epoch": new_epoch,
                    "operator": operator,
                    "reason": reason,
                    "confirmed": True,
                },
                event_id=self._membership_event_id(
                    EventType.PEER_EVICTED, node_id, new_epoch
                ),
            )
        for member in add:
            await self._append_event(
                EventType.PEER_JOINED,
                new_epoch,
                {"node_id": member.node_id, "epoch": new_epoch},
                event_id=self._membership_event_id(
                    EventType.PEER_JOINED, member.node_id, new_epoch
                ),
            )
        for node_id, _ in rescope:
            await self._append_event(
                EventType.MEMBERSHIP_UPDATED,
                new_epoch,
                {"node_id": node_id, "epoch": new_epoch, "rescoped": True},
                event_id=self._membership_event_id(
                    EventType.MEMBERSHIP_UPDATED, node_id, new_epoch
                ),
            )

        return await self._store.set_membership(new_view)


# =============================================================================
# Resync : marqueur node-local sur epoch futur / bank_version manquée
# =============================================================================


class ResyncService:
    """
    Marqueur de récupération **node-local** (``node_status.json``).

    Une observation distante d'un epoch futur ou d'une bank_version supérieure
    à la nôtre signifie qu'on a manqué une transition : on passe
    ``RESYNC_REQUIRED`` (durable) plutôt que d'avancer la composition partagée
    depuis un message non vérifié. ``mark_resync_complete`` ne revient à
    ``HEALTHY`` que quand l'état local a rattrapé la cible observée.
    """

    def __init__(self, store: HivemindStateStore) -> None:
        self._store = store

    async def _append_event(
        self, event_type: EventType, payload: dict[str, Any]
    ) -> None:
        node = await self._store.get_node_identity()
        origin = node.node_id if node is not None else "unknown"
        membership = await self._store.get_membership()
        epoch = membership.epoch if membership is not None else 0
        term_state = await self._store.get_term()
        term = term_state.term if term_state is not None else 0
        await self._store.append_event(
            EventEnvelope(
                event_id=uuid.uuid4().hex,
                type=event_type,
                origin_node_id=origin,
                term=term,
                membership_epoch=epoch,
                payload=payload,
            )
        )

    async def observe_remote(
        self,
        *,
        observed_epoch: int = -1,
        observed_bank_version: int = -1,
    ) -> NodeHealth:
        """
        Confronte une observation distante à l'état local.

        Si ``observed_epoch`` > epoch local OU ``observed_bank_version`` >
        pointeur bank_version local, passe ``RESYNC_REQUIRED`` avec les valeurs
        observées + raison, et appose un event ``RESYNC_REQUIRED``. Sinon,
        no-op (retourne la santé courante telle quelle).
        """
        existing = await self._store.get_node_status()
        # UNSAFE est fail-closed et strictement plus sévère que RESYNC_REQUIRED :
        # une observation distante ne le downgrade JAMAIS (sinon un import
        # partiel / une corruption pourrait redevenir HEALTHY sans réparation).
        # Seul un recovery explicite (ex. ré-import réussi) lève UNSAFE.
        if existing is not None and (
            HiveNodeStatus(existing.status) == HiveNodeStatus.UNSAFE
        ):
            return existing
        membership = await self._store.get_membership()
        local_epoch = membership.epoch if membership is not None else 0
        pointer = await self._store.get_bank_version_pointer()
        local_bank_version = pointer.bank_version if pointer is not None else -1

        future_epoch = observed_epoch > local_epoch
        missed_bank_version = observed_bank_version > local_bank_version

        if not (future_epoch or missed_bank_version):
            return existing or NodeHealth(status=HiveNodeStatus.HEALTHY)

        # Cible de resync MONOTONE : ne jamais descendre. Une observation plus
        # basse (ou un rapport epoch-only avec -1 par défaut) ne doit pas faire
        # oublier une epoch/bank_version plus haute déjà observée — sinon
        # mark_resync_complete repasserait HEALTHY avant rattrapage complet.
        prev_epoch = -1
        prev_bank_version = -1
        if existing is not None and (
            HiveNodeStatus(existing.status) == HiveNodeStatus.RESYNC_REQUIRED
        ):
            prev_epoch = existing.observed_epoch
            prev_bank_version = existing.observed_bank_version
        target_epoch = max(observed_epoch, prev_epoch)
        target_bank_version = max(observed_bank_version, prev_bank_version)

        reasons = []
        if future_epoch:
            reasons.append(
                f"observed future epoch {observed_epoch} > local {local_epoch}"
            )
        if missed_bank_version:
            reasons.append(
                f"missed bank_version {observed_bank_version} > local "
                f"{local_bank_version}"
            )
        health = NodeHealth(
            status=HiveNodeStatus.RESYNC_REQUIRED,
            reason="; ".join(reasons),
            observed_epoch=target_epoch,
            observed_bank_version=target_bank_version,
        )
        persisted = await self._store.set_node_status(health)
        await self._append_event(
            EventType.RESYNC_REQUIRED,
            {
                "observed_epoch": observed_epoch,
                "observed_bank_version": observed_bank_version,
                "local_epoch": local_epoch,
                "local_bank_version": local_bank_version,
            },
        )
        return persisted

    async def mark_resync_complete(self) -> NodeHealth:
        """
        Repasse ``HEALTHY`` UNIQUEMENT si l'état local a rattrapé la cible
        observée (epoch local >= observed_epoch ET bank_version local >=
        observed_bank_version). Sinon, lève — on ne ment jamais sur la santé.
        """
        health = await self._store.get_node_status()
        if health is None or HiveNodeStatus(health.status) != HiveNodeStatus.RESYNC_REQUIRED:
            raise BootstrapError(
                "mark_resync_complete: local node state is not RESYNC_REQUIRED"
            )

        membership = await self._store.get_membership()
        local_epoch = membership.epoch if membership is not None else 0
        pointer = await self._store.get_bank_version_pointer()
        local_bank_version = pointer.bank_version if pointer is not None else -1

        if local_epoch < health.observed_epoch or (
            local_bank_version < health.observed_bank_version
        ):
            raise BootstrapError(
                "mark_resync_complete refused: local state has not caught up with "
                f"target (local epoch {local_epoch}/{health.observed_epoch}, "
                f"bank_version {local_bank_version}/{health.observed_bank_version})"
            )

        healthy = NodeHealth(status=HiveNodeStatus.HEALTHY, reason="resync completed")
        persisted = await self._store.set_node_status(healthy)
        await self._append_event(EventType.RESYNC_COMPLETED, {})
        return persisted


# =============================================================================
# Bootstrap : export / import (machine à états transactionnelle)
# =============================================================================


# Chemins de TÊTE de l'état Hivemind qui sont **node-locaux** et NE doivent
# jamais voyager dans un snapshot partagé : SEULES l'identité (node.json) et la
# santé (node_status.json) le sont. Tout le reste de _hivemind/ est PARTAGÉ
# (members/term/token/bank_version/commits/acks/watermarks/...).
#
# token.json est PARTAGÉ : un token FREE porte le term/fencing partagé au repos.
# L'export refuse déjà un token HELD/RELEASING (mutation en cours), donc seul un
# token FREE (ou absent) atteint l'export ; l'exclure laisserait le peer importé
# sans baseline token alors que les autres en ont une (état divergent pour #7).
#
# On compare le CHEMIN EXACT, jamais le basename : un node_id valant "node"
# produit des chemins comme _hivemind/watermarks/node.json qui sont PARTAGÉS.
_NODE_LOCAL_HIVEMIND_PATHS = frozenset(
    {
        "_hivemind/node.json",
        "_hivemind/node_status.json",
    }
)

# Maximum-width representation emitted by ``datetime.now(UTC).isoformat()``.
# Admission capacity projection uses it for every newly-created timestamp so a
# rare exact-second value (which omits ``.000000``) can never make preflight
# seven bytes smaller than the subsequent durable membership/event/manifest.
_CAPACITY_MAX_WIDTH_ISO = "2000-01-01T00:00:00.000000+00:00"

# Champs ``_meta.json`` d'IDENTITÉ locale de la cible : jamais hérités de la
# source à l'import. ``space_id`` surtout — réécrire le space_id source sur la
# cible corromprait son identité. ``graph_memory`` n'est pas listé ici car il
# est déjà exclu du snapshot (whitelist partagée) ; le merge partant de la méta
# cible le préserve donc naturellement.
_META_IDENTITY_LOCAL = frozenset({"space_id", "owner", "created_at"})
@dataclass(frozen=True)
class BootstrapSnapshot:
    """Snapshot de bootstrap transportable : manifest + contenu par fichier."""

    manifest: BootstrapManifest
    files: dict[str, str]  # path relatif -> contenu UTF-8 exact


@dataclass(frozen=True)
class ImportResult:
    """Résultat d'un import réussi."""

    target_space_id: str
    local_node_id: str
    membership_epoch: int
    bank_version: int
    commit_id: str
    node_status: HiveNodeStatus


class BootstrapService:
    """
    Export d'un snapshot versionné depuis une source, import dans une cible
    vérifiablement vierge (HIVEMIND.md §5.1).
    """

    def __init__(self, storage: StorageService) -> None:
        self._storage = storage

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    async def _validate_export_source(
        self,
        space_id: str,
        *,
        initializing_reason: str | None = None,
        max_objects: int | None = None,
        max_bytes: int | None = None,
    ) -> tuple[HivemindStateStore, NodeIdentity, MembershipView, int, str]:
        """Validate the complete source-side bootstrap authority.

        Normal export accepts only an absent/HEALTHY local marker. The sole
        exception is the validation-only Project Mesh preparation seam: it may
        inspect an exact UNSAFE marker with the caller-supplied bounded reason
        before HEALTHY is published. It does not export or transport anything.
        """

        if max_objects is not None:
            inventory_cap = max_objects + len(_NODE_LOCAL_HIVEMIND_PATHS) + 1
            inventory = await self._storage.list_objects(
                f"{space_id}/_hivemind/", max_keys=inventory_cap
            )
            shared_inventory = []
            size_floor = 0
            for obj in inventory:
                key = obj.get("Key") if isinstance(obj, dict) else None
                if not isinstance(key, str) or not key.startswith(f"{space_id}/"):
                    raise BootstrapLimitError(
                        "export Hivemind inventory is malformed"
                    )
                rel = key[len(space_id) + 1 :]
                if not self._is_shared_export_path(rel):
                    continue
                shared_inventory.append(obj)
                size_hint = obj.get("Size")
                if max_bytes is not None:
                    if type(size_hint) is not int or size_hint < 0:
                        raise BootstrapLimitError(
                            "export object size metadata is invalid"
                        )
                    if size_hint > max_bytes:
                        raise BootstrapLimitError(
                            "export object exceeds the byte bound"
                        )
                    if rel != "_hivemind/members.json":
                        size_floor += size_hint
                    if size_floor > max_bytes:
                        raise BootstrapLimitError(
                            "export exceeds the byte bound"
                        )
            if len(shared_inventory) > max_objects or len(inventory) >= inventory_cap:
                raise BootstrapLimitError(
                    "export Hivemind inventory exceeds its safety bound"
                )

        store = HivemindStateStore(storage=self._storage, space_id=space_id)
        node = await store.get_node_identity()
        if node is None:
            raise BootstrapError(
                f"export impossible: {space_id!r} has no Hivemind identity"
            )
        token = await store.get_token()
        if token is not None and token.state in (
            TokenState.HELD.value,
            TokenState.RELEASING.value,
        ):
            raise BootstrapError(
                f"export refused: token {token.state!r} on {space_id!r} "
                "(mutation in progress)"
            )

        source_health = await store.get_node_status()
        if initializing_reason is None:
            health_allowed = source_health is None or (
                HiveNodeStatus(source_health.status) == HiveNodeStatus.HEALTHY
            )
        else:
            health_allowed = source_health is not None and (
                HiveNodeStatus(source_health.status) == HiveNodeStatus.UNSAFE
                and source_health.reason == initializing_reason
            )
        if not health_allowed:
            status = source_health.status if source_health is not None else "absent"
            if initializing_reason is None:
                operation = "export"
                health_requirement = "bootstrap only from a HEALTHY source"
            else:
                operation = "source preparation validation"
                health_requirement = (
                    "requires the exact initializing UNSAFE marker"
                )
            raise BootstrapError(
                f"{operation} refused: source {space_id!r} is in state "
                f"{status!r} ({health_requirement})"
            )

        membership = await store.get_membership()
        if not active_members(membership):
            raise BootstrapError(
                f"export refused: {space_id!r} has no MembershipView with "
                "ACTIVE members — bootstrap impossible (HIVEMIND.md §5.1.5)"
            )
        assert membership is not None  # narrowed by active_members above
        source_member = next(
            (
                m
                for m in membership.members
                if m.node_id == node.node_id
                and m.status == MemberStatus.ACTIVE.value
            ),
            None,
        )
        if source_member is None:
            raise BootstrapError(
                f"export refused: source node {node.node_id!r} is not an "
                "ACTIVE member of MembershipView (evicted?)"
            )
        if not source_member.public_key or source_member.public_key != node.public_key:
            raise BootstrapError(
                f"export refused: source member {node.node_id!r} has no "
                "public_key consistent with node.json — an imported peer could "
                "not authenticate the source (UNKNOWN_PEER)"
            )

        pointer = await store.get_bank_version_pointer()
        bank_version = pointer.bank_version if pointer is not None else -1
        commit_id = pointer.commit_id if pointer is not None else ""
        try:
            await _validate_full_hivemind_state(store, self._storage, space_id)
        except CorruptedStateError as exc:
            raise BootstrapError(
                f"export refused: invalid source Hivemind state ({exc})"
            ) from exc
        if bank_version >= 0:
            src_commit = await store.get_commit(bank_version)
            if src_commit is None or src_commit.commit_id != commit_id:
                raise BootstrapError(
                    "export refused: inconsistent source bank_version pointer — "
                    f"commit {bank_version} absent or commit_id diverges"
                )
        return store, node, membership, bank_version, commit_id

    async def _collect_shared_export_files(
        self,
        space_id: str,
        *,
        max_objects: int | None = None,
        max_bytes: int | None = None,
    ) -> tuple[dict[str, str], list[BootstrapManifestEntry]]:
        """Read and project every path that a bootstrap would export."""

        files: dict[str, str] = {}
        entries: list[BootstrapManifestEntry] = []
        prefix = f"{space_id}/"
        projected_bytes = 0

        async def add_object(obj: dict) -> None:
            nonlocal projected_bytes
            key = obj["Key"]
            rel = key[len(prefix):]
            if not self._is_shared_export_path(rel):
                return
            if rel in files:
                return
            if max_objects is not None and len(files) >= max_objects:
                raise BootstrapLimitError("export exceeds the object-count bound")
            size_hint = obj.get("Size")
            if max_bytes is not None:
                if type(size_hint) is not int or size_hint < 0:
                    raise BootstrapLimitError(
                        "export object size metadata is invalid"
                    )
                if size_hint > max_bytes:
                    raise BootstrapLimitError("export object exceeds the byte bound")
                if (
                    rel not in {"_meta.json", "_hivemind/members.json"}
                    and projected_bytes + size_hint > max_bytes
                ):
                    raise BootstrapLimitError("export exceeds the byte bound")
            content = await self._storage.get(key)
            if content is None:
                return
            content = self._project_export_content(rel, content)
            files[rel] = content
            raw = content.encode("utf-8")
            projected_bytes += len(raw)
            if max_bytes is not None and projected_bytes > max_bytes:
                raise BootstrapLimitError("export exceeds the byte bound")
            entries.append(
                BootstrapManifestEntry(
                    path=rel, sha256=_sha256_bytes(raw), size=len(raw)
                )
            )

        # Top-level shared files are exact keys. Listing each exact prefix gives
        # a Size precheck without walking unrelated graph/local objects.
        for rel in ("_meta.json", "_rules.md", "_synthesis.md"):
            exact_key = prefix + rel
            matches = await self._storage.list_objects(exact_key, max_keys=2)
            exact = next(
                (
                    obj
                    for obj in matches
                    if isinstance(obj, dict) and obj.get("Key") == exact_key
                ),
                None,
            )
            if exact is not None:
                await add_object(exact)

        async def add_prefix(rel_prefix: str, *, allowance: int) -> None:
            remaining = 0 if max_objects is None else max_objects - len(files)
            raw_cap = 0 if max_objects is None else remaining + allowance + 1
            objects = await self._storage.list_objects(
                prefix + rel_prefix, max_keys=raw_cap
            )
            before = len(files)
            for obj in objects:
                if not isinstance(obj, dict) or not isinstance(obj.get("Key"), str):
                    raise BootstrapLimitError("export object inventory is malformed")
                await add_object(obj)
            if raw_cap and len(objects) >= raw_cap and len(files) - before <= remaining:
                # The bounded page was exhausted without proving that every
                # later object is excluded. Never return a partial snapshot.
                raise BootstrapLimitError(
                    "export object inventory exceeds its safety bound"
                )

        await add_prefix("bank/", allowance=1)
        await add_prefix("live/", allowance=1)
        await add_prefix(
            "_hivemind/", allowance=len(_NODE_LOCAL_HIVEMIND_PATHS)
        )
        entries.sort(key=lambda entry: entry.path)
        return files, entries

    @classmethod
    def _project_export_content(cls, rel: str, content: str) -> str:
        """Apply the exact per-path projection used by every snapshot."""

        if rel == "_meta.json":
            return cls._project_meta(content)
        if rel == "_hivemind/members.json":
            return cls._project_members(content)
        return content

    @staticmethod
    def _storage_json(model: Any) -> str:
        """Serialize a model exactly like ``StorageService.put_json``."""

        return json.dumps(
            model.model_dump(mode="json"), indent=2, ensure_ascii=False
        )

    @staticmethod
    def _snapshot_from_files(
        files: dict[str, str],
        *,
        source_node_id: str,
        membership_epoch: int,
        bank_version: int,
        commit_id: str,
    ) -> BootstrapSnapshot:
        entries = []
        for path, content in sorted(files.items()):
            raw = content.encode("utf-8")
            entries.append(
                BootstrapManifestEntry(
                    path=path, sha256=_sha256_bytes(raw), size=len(raw)
                )
            )
        manifest = BootstrapManifest(
            source_node_id=source_node_id,
            membership_epoch=membership_epoch,
            bank_version=bank_version,
            commit_id=commit_id,
            entries=entries,
        )
        manifest.manifest_sha256 = manifest_content_hash(manifest)
        return BootstrapSnapshot(manifest=manifest, files=files)

    async def project_source_preparation_snapshot(
        self,
        space_id: str,
        *,
        node: NodeIdentity,
        membership: MembershipView,
        term: TermState,
        token: TokenLeaseState,
        pointer: BankVersionPointer,
        max_objects: int | None = None,
        max_bytes: int | None = None,
    ) -> BootstrapSnapshot:
        """Project the exact snapshot that source genesis would later export.

        The current business files use the normal export collector (including
        ``_meta`` projection). The four shared genesis models are serialized as
        ``put_json`` will persist them, then passed through the same members
        projection as a real export. Node identity and health stay node-local.
        This method is read-only and is therefore safe before the durable intent.
        """

        files, _entries = await self._collect_shared_export_files(
            space_id, max_objects=max_objects, max_bytes=max_bytes
        )
        projected_models = {
            "_hivemind/members.json": membership,
            "_hivemind/term.json": term,
            "_hivemind/token.json": token,
            "_hivemind/bank_version.json": pointer,
        }
        for rel, model in projected_models.items():
            files[rel] = self._project_export_content(
                rel, self._storage_json(model)
            )
        projected = self._snapshot_from_files(
            files,
            source_node_id=node.node_id,
            membership_epoch=membership.epoch,
            bank_version=pointer.bank_version,
            commit_id=pointer.commit_id,
        )
        manifest = projected.manifest.model_copy(
            update={"created_at": _CAPACITY_MAX_WIDTH_ISO}
        )
        return BootstrapSnapshot(manifest=manifest, files=projected.files)

    async def validate_source_preparation(
        self,
        space_id: str,
        *,
        initializing_reason: str,
        max_objects: int | None = None,
        max_bytes: int | None = None,
    ) -> BootstrapSnapshot:
        """Validate an exact initializing source before publishing HEALTHY.

        This method is deliberately validation-only. It accepts only the exact
        UNSAFE reason supplied by the preparation service, validates all normal
        export authority and parses every shared export path, then discards the
        in-memory projection.
        """

        if type(initializing_reason) is not str or not initializing_reason:
            raise BootstrapError("source preparation reason is required")
        _store, node, membership, bank_version, commit_id = await self._validate_export_source(
            space_id,
            initializing_reason=initializing_reason,
            max_objects=max_objects,
            max_bytes=max_bytes,
        )
        files, _entries = await self._collect_shared_export_files(
            space_id, max_objects=max_objects, max_bytes=max_bytes
        )
        return self._snapshot_from_files(
            files,
            source_node_id=node.node_id,
            membership_epoch=membership.epoch,
            bank_version=bank_version,
            commit_id=commit_id,
        )

    def project_membership_admission_snapshot(
        self,
        snapshot: BootstrapSnapshot,
        *,
        space_id: str,
        candidate: Member,
        source_node: NodeIdentity,
        term: TermState,
    ) -> BootstrapSnapshot:
        """Project the exact-size e+1 export produced by candidate admission.

        Admission replaces ``members.json`` and appends one deterministic
        membership event. Generated timestamps have the repository's fixed ISO
        representation, so their values may differ at apply time but their JSON
        and manifest path lengths do not; the serialized capacity proof is exact.
        """

        members_path = "_hivemind/members.json"
        raw_members = snapshot.files.get(members_path)
        if raw_members is None:
            raise BootstrapError("admission projection requires members.json")
        current = MembershipView.model_validate_json(raw_members)
        next_members = [
            member for member in current.members if member.node_id != candidate.node_id
        ]
        next_members.append(
            candidate.model_copy(update={"status": MemberStatus.PENDING.value})
        )
        new_epoch = current.epoch + 1
        projected_view = MembershipView(
            epoch=new_epoch,
            members=next_members,
            updated_at=_CAPACITY_MAX_WIDTH_ISO,
        )
        event = EventEnvelope(
            event_id=uuid.uuid5(
                uuid.NAMESPACE_OID,
                f"{space_id}:{EventType.MEMBERSHIP_UPDATED.value}:"
                f"{candidate.node_id}:{new_epoch}",
            ).hex,
            type=EventType.MEMBERSHIP_UPDATED,
            origin_node_id=source_node.node_id,
            term=term.term,
            membership_epoch=new_epoch,
            created_at=_CAPACITY_MAX_WIDTH_ISO,
            payload={
                "node_id": candidate.node_id,
                "epoch": new_epoch,
                "status": MemberStatus.PENDING.value,
            },
        )
        files = dict(snapshot.files)
        files[members_path] = self._project_members(
            self._storage_json(projected_view)
        )
        event_suffix = f"_{event.event_id}.json"
        event_already_persisted = any(
            path.startswith("_hivemind/events/") and path.endswith(event_suffix)
            for path in files
        )
        if not event_already_persisted:
            event_key = layout.event_key(space_id, event.created_at, event.event_id)
            files[event_key[len(space_id) + 1 :]] = self._storage_json(event)
        projected = self._snapshot_from_files(
            files,
            source_node_id=source_node.node_id,
            membership_epoch=new_epoch,
            bank_version=snapshot.manifest.bank_version,
            commit_id=snapshot.manifest.commit_id,
        )
        manifest = projected.manifest.model_copy(
            update={"created_at": _CAPACITY_MAX_WIDTH_ISO}
        )
        return BootstrapSnapshot(manifest=manifest, files=projected.files)

    async def validate_export_authority(
        self,
        space_id: str,
        *,
        max_objects: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        """Validate source protocol authority without materializing a snapshot.

        Admin source-readiness uses this bounded protocol-state check. The
        later approval/export still reads and hashes every shared object, while
        the preparation transition uses :meth:`validate_source_preparation` to
        perform that full read before its one-time HEALTHY publication.
        """

        await self._validate_export_source(
            space_id, max_objects=max_objects, max_bytes=max_bytes
        )

    async def export_snapshot(
        self,
        space_id: str,
        *,
        max_objects: int | None = None,
        max_bytes: int | None = None,
    ) -> BootstrapSnapshot:
        """
        Exporte tous les fichiers partagés du space source en un snapshot
        versionné + manifest (sha256 par-fichier + manifest_sha256).

        Refuse si le token source est ``HELD``/``RELEASING`` (HIVEMIND.md
        §5.1 étape 4 : pas de snapshot pendant une mutation en cours).

        ``_meta.json`` est projeté sur la whitelist partagée (graph_memory
        exclu). Les fichiers ``_hivemind/`` node-locaux (node/node_status/
        token) sont exclus ; le reste de l'état (members/term/bank_version/
        commits/...) est inclus pour préserver epoch + bank_version source.
        """
        _store, node, membership, bank_version, commit_id = (
            await self._validate_export_source(
                space_id, max_objects=max_objects, max_bytes=max_bytes
            )
        )
        membership_epoch = membership.epoch
        files, _entries = await self._collect_shared_export_files(
            space_id, max_objects=max_objects, max_bytes=max_bytes
        )

        # NB : l'audit d'export/import bootstrap est NODE-LOCAL et n'est PAS
        # écrit dans le journal partagé `events/`. Un event ajouté côté source
        # APRÈS la collecte du snapshot (ou côté cible à l'import) ne serait
        # jamais vu par l'autre noeud et ferait diverger en permanence l'état
        # d'audit/dedup partagé. La traçabilité du bootstrap passe par
        # node_status + le BankVersionPointer + l'ImportResult.
        return self._snapshot_from_files(
            files,
            source_node_id=node.node_id,
            membership_epoch=membership_epoch,
            bank_version=bank_version,
            commit_id=commit_id,
        )

    @staticmethod
    def _is_shared_export_path(rel: str) -> bool:
        """True si un chemin relatif d'objet source doit voyager dans le
        snapshot partagé."""
        if rel == "" or rel.endswith("/.keep"):
            return False
        if rel == "_meta.json" or rel == "_rules.md" or rel == "_synthesis.md":
            return True
        if rel.startswith("bank/") or rel.startswith("live/"):
            return True
        if rel.startswith("_hivemind/"):
            return rel not in _NODE_LOCAL_HIVEMIND_PATHS
        return False

    @staticmethod
    def _project_meta(raw: str) -> str:
        meta = json.loads(raw)
        projected = meta_shared_projection(meta) or {}
        # Sérialisation déterministe pour un hash stable cross-host.
        return json.dumps(projected, ensure_ascii=False, sort_keys=True, indent=2)

    @staticmethod
    def _project_members(raw: str) -> str:
        """Retire le tag ``incarnation`` (métadonnée source-locale P10-3) de chaque
        membre avant export du snapshot partagé. La cible n'en a pas besoin (seule
        la source force-évince), et l'absence du champ garde le members.json exporté
        identique à un lecteur pré-P10-3 (``extra='forbid'``). Neutre pour la
        convergence : ``candidate_view_digest`` exclut déjà ``incarnation``."""
        view = MembershipView.model_validate_json(raw)
        stripped = view.model_copy(
            update={
                "members": [m.model_copy(update={"incarnation": None}) for m in view.members]
            }
        )
        return stripped.model_dump_json()

    async def _merge_target_meta(
        self, target_space_id: str, source_meta_json: str
    ) -> str:
        """
        Fusionne la projection partagée de la source DANS la méta existante de
        la cible — sans rien écraser de node-local.

        On part de la méta cible (préserve ``graph_memory`` et tout champ
        inconnu), on superpose les champs descriptifs hérités de la source
        (description, version, compteurs de consolidation), mais on N'HÉRITE
        JAMAIS des champs d'identité (``_META_IDENTITY_LOCAL``) et on force le
        ``space_id`` de la CIBLE. Réécrire le space_id source sur la cible
        corromprait son identité (HIVEMIND.md §3.4).

        Defense-in-depth : la source est d'abord projetée via l'allowlist
        partagée (``meta_shared_projection``, default-exclude) AVANT le merge —
        l'import ne fait JAMAIS confiance à la discipline de l'exporteur. Un
        snapshot d'une source boguée/ancienne/malveillante contenant un bloc
        ``graph_memory`` ne peut donc pas écraser le ``graph_memory`` local de
        la cible.
        """
        source = meta_shared_projection(json.loads(source_meta_json)) or {}
        existing_raw = await self._storage.get(f"{target_space_id}/_meta.json")
        target = json.loads(existing_raw) if existing_raw else {}
        merged = dict(target)
        for key, value in source.items():
            if key in _META_IDENTITY_LOCAL:
                continue
            merged[key] = value
        merged["space_id"] = target_space_id
        return json.dumps(merged, ensure_ascii=False, sort_keys=True, indent=2)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    async def import_snapshot(
        self,
        target_space_id: str,
        snapshot: BootstrapSnapshot,
        local_keypair: Any,
    ) -> ImportResult:
        """
        Importe un snapshot dans une cible vierge — machine à états
        transactionnelle.

        Étapes :
        1. valider que la cible est vierge (placeholders uniquement) ;
        2. vérifier protocol_version + manifest_sha256 + chaque sha256
           par-fichier AVANT tout write ;
        3. passer ``node_status=UNSAFE`` (reason=bootstrap_in_progress) ;
        4. écrire les fichiers du space (bank/live/rules/synthesis/_meta) ;
        5. écrire l'état Hivemind partagé (members/term/bank_version/commits)
           en préservant epoch + bank_version source (``_meta.json`` est MERGÉ,
           pas remplacé : la cible garde son identité locale) ;
        6. adopter l'identité locale PRÉ-PROVISIONNÉE : le node_id du membre
           dont la clé publique == la nôtre dans la MembershipView importée ;
        7. re-vérifier que le pointeur bank_version == manifest ;
        8. seulement alors ``node_status=HEALTHY`` + event IMPORTED.

        Tout échec après l'étape 3 laisse ``node_status=UNSAFE`` : un import
        crashé ne se relit jamais comme sain.

        ``local_keypair`` : objet portant ``.public_key`` (cf.
        ``generate_peer_keypair``). Le peer doit avoir été ajouté à la
        MembershipView source AVANT l'export (HIVEMIND.md §5.1.5 : tous les
        participants persistent la même MembershipView) ; l'import retrouve son
        node_id par correspondance de clé publique et refuse fail-closed sinon.
        Le node_id source n'est JAMAIS réutilisé.
        """
        manifest = snapshot.manifest
        if manifest.protocol_version != layout.PROTOCOL_VERSION:
            raise BootstrapError(
                f"incompatible protocol_version: manifest "
                f"{manifest.protocol_version} != {layout.PROTOCOL_VERSION}"
            )

        # 1. Cible vierge ?
        await self._assert_blank_target(target_space_id)

        # 2. Vérification d'intégrité AVANT tout write.
        self._verify_manifest(snapshot)

        # 2b. Defense-in-depth : ré-appliquer l'allowlist de chemins partagés sur
        #     le manifest reçu — ne JAMAIS faire confiance à la discipline de
        #     l'exporteur. Un snapshot ne doit pas semer d'état node-local
        #     (_hivemind/node.json|node_status.json|token.json) ni un chemin non
        #     partagé : écrit verbatim, il pourrait planter un token bidon ou
        #     écraser le marqueur UNSAFE avant la fin de la validation.
        for entry in manifest.entries:
            if not self._is_shared_export_path(entry.path):
                raise BootstrapError(
                    f"import refused: non-shareable path in snapshot "
                    f"{entry.path!r} (a peer never seeds node-local state)"
                )

        store = HivemindStateStore(storage=self._storage, space_id=target_space_id)

        # 3. UNSAFE avant le moindre write d'objet.
        await store.set_node_status(
            NodeHealth(
                status=HiveNodeStatus.UNSAFE, reason="bootstrap_in_progress"
            )
        )

        # 4. Écrire les fichiers du space + l'état Hivemind partagé via put()
        #    (pas copy_object) — les checksums ont été vérifiés en-process.
        #    `_meta.json` est MERGÉ dans la méta cible (pas remplacé) : on
        #    préserve les champs node-locaux (graph_memory, owner, created_at)
        #    et surtout le `space_id` de la CIBLE — jamais celui de la source.
        prefix = f"{target_space_id}/"
        for entry in manifest.entries:
            content = snapshot.files[entry.path]
            if entry.path == "_meta.json":
                content = await self._merge_target_meta(target_space_id, content)
            await self._storage.put(prefix + entry.path, content)

        # 5/6. Adopter l'identité locale PRÉ-PROVISIONNÉE : le peer doit déjà
        #      figurer comme membre ACTIVE de la MembershipView importée
        #      (HIVEMIND.md §5.1.5 : « tous les participants persistent la même
        #      MembershipView »). On retrouve notre node_id via la clé publique
        #      locale — la source ne pouvait pas connaître un UUID tiré au hasard,
        #      donc un node aléatoire serait rejeté UNKNOWN_PEER et absent des
        #      attentes d'ACK. Sans correspondance : refus fail-closed (UNSAFE).
        local_public_key = getattr(local_keypair, "public_key", "")
        membership = await store.get_membership()
        # Intégrité de la membership importée : tout membre ACTIVE doit avoir une
        # public_key non-vide, et les node_id / public_key actifs doivent être
        # UNIQUES sur TOUTE la membership. Sinon une seule clé privée
        # authentifierait plusieurs node_ids (peer._verify cherche par node_id
        # puis vérifie la clé stockée) — usurpation / corruption de l'identité
        # ACK. On ne fait JAMAIS confiance à la membership d'un snapshot d'une
        # source inconnue/corrompue/malveillante.
        active_imported = [
            m
            for m in (membership.members if membership is not None else [])
            if m.status == MemberStatus.ACTIVE.value
        ]
        if any(not m.public_key for m in active_imported):
            raise BootstrapError(
                "import refused: imported ACTIVE member has no public_key"
            )
        active_node_ids = [m.node_id for m in active_imported]
        active_pubkeys = [m.public_key for m in active_imported]
        if len(set(active_node_ids)) != len(active_node_ids):
            raise BootstrapError(
                "import refused: duplicate ACTIVE node_id in imported membership"
            )
        if len(set(active_pubkeys)) != len(active_pubkeys):
            raise BootstrapError(
                "import refused: duplicate ACTIVE public_key in imported membership "
                "— one key would authenticate multiple nodes"
            )
        # Cohérence membership <-> headers du manifest : le hash couvre les
        # headers, mais pas leur ACCORD avec le CONTENU de members.json. Un
        # snapshot recomputé après tampering / un exporteur bogué pourrait avoir
        # membership.epoch != manifest.membership_epoch, ou un source_node_id
        # absent des membres ACTIVE — le peer ne pourrait alors plus authentifier
        # la source ni raisonner sur l'epoch attendu.
        if membership is None:
            raise BootstrapError(
                "import refused: membership absent after writing snapshot"
            )
        if membership.epoch != manifest.membership_epoch:
            raise BootstrapError(
                f"import refused: imported epoch {membership.epoch} != manifest "
                f"header {manifest.membership_epoch}"
            )
        if not any(
            m.node_id == manifest.source_node_id
            and m.status == MemberStatus.ACTIVE.value
            for m in membership.members
        ):
            raise BootstrapError(
                "import refused: manifest source_node_id "
                f"{manifest.source_node_id!r} is not an imported ACTIVE member "
                "— the peer could not authenticate the bootstrap source"
            )
        # Miroir du refus d'export : un peer vierge ne doit JAMAIS hériter d'une
        # mutation en cours. Un snapshot d'un peer bogué/ancien pourrait porter
        # un token.json HELD/RELEASING (token.json est partagé) — on refuse.
        imported_token = await store.get_token()
        if imported_token is not None and imported_token.state in (
            TokenState.HELD.value,
            TokenState.RELEASING.value,
        ):
            raise BootstrapError(
                f"import refused: imported token {imported_token.state!r} — an empty "
                "peer must not inherit a mutation in progress (HELD/RELEASING)"
            )
        matches = []
        if membership is not None and local_public_key:
            matches = [
                m
                for m in membership.members
                if m.public_key == local_public_key
                and m.status == MemberStatus.ACTIVE.value
            ]
        # EXACTEMENT une correspondance : 0 = peer non provisionné (HIVEMIND.md
        # §5.1.5) ; >=2 = snapshot ambigu (source ancienne/corrompue/malveillante
        # avec des clés actives dupliquées) — choisir « le premier » adopterait
        # un node_id dépendant de l'ordre. Dans les deux cas, refus fail-closed.
        if len(matches) != 1:
            raise BootstrapError(
                "import refused: local public key must match EXACTLY one ACTIVE "
                f"member in imported MembershipView ({len(matches)} match(es)) "
                "— peer not provisioned or snapshot ambiguous"
            )
        match = matches[0]
        if match.node_id == manifest.source_node_id:
            raise BootstrapError(
                "import refused: local identity matches the source node "
                "(an empty peer never reuses the source identity)"
            )
        local_node_id = match.node_id
        await store.set_node_identity(
            NodeIdentity(node_id=local_node_id, public_key=local_public_key)
        )

        # 7. Re-vérifier le pointeur bank_version == manifest.
        pointer = await store.get_bank_version_pointer()
        observed_bv = pointer.bank_version if pointer is not None else -1
        observed_commit = pointer.commit_id if pointer is not None else ""
        if observed_bv != manifest.bank_version:
            # Laisse UNSAFE — ne flip jamais HEALTHY.
            raise BootstrapError(
                "imported bank_version is inconsistent with manifest: "
                f"{observed_bv} != {manifest.bank_version}"
            )
        if manifest.bank_version >= 0:
            commit = await store.get_commit(manifest.bank_version)
            if commit is None or commit.commit_id != manifest.commit_id:
                raise BootstrapError(
                    "bank_version commit is absent or commit_id diverges "
                    f"for bank_version={manifest.bank_version}"
                )

        # 7b. Valider TOUT l'état Hivemind importé avant de passer HEALTHY. Un
        #     fichier checksum-valide mais sémantiquement corrompu (term.json,
        #     queue, ack, watermark, event copiés d'une source endommagée)
        #     passerait sinon HEALTHY puis lèverait CorruptedStateError au
        #     premier cold-start / opération pair. load_snapshot + list_events +
        #     list_acks désérialisent chaque fichier critique et lèvent sur
        #     invalidité ; on laisse alors UNSAFE.
        try:
            await _validate_full_hivemind_state(store, self._storage, target_space_id)
        except CorruptedStateError as exc:
            raise BootstrapError(
                f"import refused: invalid imported Hivemind state ({exc}) — "
                "left UNSAFE"
            ) from exc

        # 8. node_status HEALTHY = DERNIER write durable : tout échec antérieur
        #    laisse la cible UNSAFE (fail-closed). L'audit d'import est
        #    NODE-LOCAL et n'est PAS écrit dans le journal partagé `events/` —
        #    un event IMPORTED présent côté cible mais absent côté source
        #    ferait diverger l'audit/dedup partagé. La traçabilité passe par
        #    node_status + l'ImportResult.
        membership = await store.get_membership()
        epoch = membership.epoch if membership is not None else 0
        await store.set_node_status(
            NodeHealth(status=HiveNodeStatus.HEALTHY, reason="bootstrap imported")
        )
        return ImportResult(
            target_space_id=target_space_id,
            local_node_id=local_node_id,
            membership_epoch=epoch,
            bank_version=observed_bv,
            commit_id=observed_commit,
            node_status=HiveNodeStatus.HEALTHY,
        )

    async def import_pending_snapshot(
        self,
        target_space_id: str,
        snapshot: BootstrapSnapshot,
        local_keypair: Any,
    ) -> ImportResult:
        """Import bootstrap Project Mesh d'une cible admise PENDING à e+1 (P10-3).

        Sœur de :meth:`import_snapshot` réutilisant VERBATIM ses étapes 1-6, 7,
        7b et ses helpers privés, avec DEUX seules différences imposées par le
        contrat frozen (PROJECT_MESH.md §3, ADR-0024) :

        1. l'identité locale est adoptée depuis le membre **PENDING** (et non
           ACTIVE) dont la clé publique == la nôtre. Unicité node_id ET clé
           imposée sur ACTIVE ∪ PENDING ; la clé locale ne doit JAMAIS
           correspondre à un membre ACTIVE (anti-usurpation : une cible ne peut
           adopter une identité active) ; le node source n'est jamais réutilisé.
        2. ``node_status`` reste **UNSAFE** (jamais HEALTHY) : une cible PENDING
           route REFUSE (defense-in-depth derrière le garde de réservation) tant
           que le bump e+2 répliqué n'a pas été appliqué. Le flip HEALTHY est
           fait, en dernier, par la self-activation e+2 (``apply_self_activation``
           côté cible), pas ici.

        Tout échec après l'étape 3 laisse la cible UNSAFE (fail-closed).
        """
        manifest = snapshot.manifest
        if manifest.protocol_version != layout.PROTOCOL_VERSION:
            raise BootstrapError(
                f"incompatible protocol_version: manifest "
                f"{manifest.protocol_version} != {layout.PROTOCOL_VERSION}"
            )

        # 1-2. Cible vierge + intégrité AVANT tout write (helpers partagés).
        await self._assert_blank_target(target_space_id)
        self._verify_manifest(snapshot)
        for entry in manifest.entries:
            if not self._is_shared_export_path(entry.path):
                raise BootstrapError(
                    f"import refused: non-shareable path in snapshot "
                    f"{entry.path!r} (a peer never seeds node-local state)"
                )

        store = HivemindStateStore(storage=self._storage, space_id=target_space_id)

        # 3. UNSAFE avant le moindre write d'objet.
        await store.set_node_status(
            NodeHealth(status=HiveNodeStatus.UNSAFE, reason="mesh_pending_activation")
        )

        # 4. Écrire les fichiers du space + état partagé (_meta.json mergé).
        prefix = f"{target_space_id}/"
        for entry in manifest.entries:
            content = snapshot.files[entry.path]
            if entry.path == "_meta.json":
                content = await self._merge_target_meta(target_space_id, content)
            await self._storage.put(prefix + entry.path, content)

        # 5/6. Intégrité de la membership importée sur ACTIVE ∪ PENDING (mêmes
        #      garanties d'unicité que import_snapshot, étendues au candidat) et
        #      adoption de l'identité PENDING pré-provisionnée.
        local_public_key = getattr(local_keypair, "public_key", "")
        membership = await store.get_membership()
        if membership is None:
            raise BootstrapError(
                "import refused: membership absent after writing snapshot"
            )
        roster = [
            m
            for m in membership.members
            if m.status
            in (MemberStatus.ACTIVE.value, MemberStatus.PENDING.value)
        ]
        if any(not m.public_key for m in roster):
            raise BootstrapError(
                "import refused: imported ACTIVE/PENDING member has no "
                "public_key"
            )
        roster_node_ids = [m.node_id for m in roster]
        roster_pubkeys = [m.public_key for m in roster]
        if len(set(roster_node_ids)) != len(roster_node_ids):
            raise BootstrapError(
                "import refused: duplicate node_id in imported membership "
                "(ACTIVE ∪ PENDING)"
            )
        if len(set(roster_pubkeys)) != len(roster_pubkeys):
            raise BootstrapError(
                "import refused: duplicate public_key in imported membership "
                "(ACTIVE ∪ PENDING) — one key would authenticate multiple nodes"
            )
        if membership.epoch != manifest.membership_epoch:
            raise BootstrapError(
                f"import refused: imported epoch {membership.epoch} != manifest "
                f"header {manifest.membership_epoch}"
            )
        if not any(
            m.node_id == manifest.source_node_id
            and m.status == MemberStatus.ACTIVE.value
            for m in membership.members
        ):
            raise BootstrapError(
                "import refused: manifest source_node_id "
                f"{manifest.source_node_id!r} is not an imported ACTIVE member"
            )
        imported_token = await store.get_token()
        if imported_token is not None and imported_token.state in (
            TokenState.HELD.value,
            TokenState.RELEASING.value,
        ):
            raise BootstrapError(
                f"import refused: imported token {imported_token.state!r} — an empty "
                "peer must not inherit a mutation in progress"
            )
        # Anti-usurpation : la clé locale ne doit JAMAIS coïncider avec un membre
        # ACTIVE (la cible ne peut adopter une identité active), et doit
        # correspondre à EXACTEMENT un membre PENDING (0 = non provisionné,
        # >=2 = ambigu).
        if local_public_key and any(
            m.public_key == local_public_key
            and m.status == MemberStatus.ACTIVE.value
            for m in membership.members
        ):
            raise BootstrapError(
                "import refused: local public key matches an ACTIVE member "
                "(a PENDING target never adopts an active identity)"
            )
        matches = []
        if local_public_key:
            matches = [
                m
                for m in membership.members
                if m.public_key == local_public_key
                and m.status == MemberStatus.PENDING.value
            ]
        if len(matches) != 1:
            raise BootstrapError(
                "import refused: local public key must match EXACTLY one PENDING "
                f"member in imported MembershipView ({len(matches)} match(es)) "
                "— peer not provisioned or snapshot ambiguous"
            )
        match = matches[0]
        if match.node_id == manifest.source_node_id:
            raise BootstrapError(
                "import refused: local identity matches the source node"
            )
        local_node_id = match.node_id
        await store.set_node_identity(
            NodeIdentity(node_id=local_node_id, public_key=local_public_key)
        )

        # 7. Re-vérifier le pointeur bank_version == manifest.
        pointer = await store.get_bank_version_pointer()
        observed_bv = pointer.bank_version if pointer is not None else -1
        observed_commit = pointer.commit_id if pointer is not None else ""
        if observed_bv != manifest.bank_version:
            raise BootstrapError(
                "imported bank_version is inconsistent with manifest: "
                f"{observed_bv} != {manifest.bank_version}"
            )
        if manifest.bank_version >= 0:
            commit = await store.get_commit(manifest.bank_version)
            if commit is None or commit.commit_id != manifest.commit_id:
                raise BootstrapError(
                    "bank_version commit is absent or commit_id diverges "
                    f"for bank_version={manifest.bank_version}"
                )

        # 7b. Valider tout l'état importé (laisse UNSAFE sur invalidité).
        try:
            await _validate_full_hivemind_state(store, self._storage, target_space_id)
        except CorruptedStateError as exc:
            raise BootstrapError(
                f"import refused: invalid imported Hivemind state ({exc}) — "
                "left UNSAFE"
            ) from exc

        # 8. PAS de flip HEALTHY : la cible reste UNSAFE (route REFUSE) jusqu'à la
        #    self-activation e+2. C'est la SEULE différence d'issue avec
        #    import_snapshot.
        membership = await store.get_membership()
        epoch = membership.epoch if membership is not None else 0
        return ImportResult(
            target_space_id=target_space_id,
            local_node_id=local_node_id,
            membership_epoch=epoch,
            bank_version=observed_bv,
            commit_id=observed_commit,
            node_status=HiveNodeStatus.UNSAFE,
        )

    async def _assert_blank_target(self, target_space_id: str) -> None:
        """
        Refuse si la cible contient autre chose que des placeholders.

        Autorisé : ``_meta.json``, ``_rules.md``, ``live/.keep``,
        ``bank/.keep``. Refusé : tout ``bank/*`` ou ``live/*`` non-.keep,
        ``_synthesis.md``, et TOUT objet ``_hivemind/*`` (un space déjà dans
        un cluster ne doit pas être réimporté — anti cross-cluster merge).
        """
        prefix = f"{target_space_id}/"
        # Five placeholder keys (including an optional S3 directory marker) are
        # allowed; the sixth necessarily proves non-blank. Never slurp an
        # attacker-sized prefix.
        objects = await self._storage.list_objects(prefix, max_keys=6)
        offending: list[str] = []
        for obj in objects:
            rel = obj["Key"][len(prefix):]
            if rel in ("", "_meta.json", "_rules.md", "live/.keep", "bank/.keep"):
                continue
            # node_status.json écrit par un import précédent crashé compte aussi.
            offending.append(rel)
        if offending:
            raise BootstrapError(
                f"import refused: target {target_space_id!r} is not empty "
                f"(non-placeholder objects: {sorted(offending)[:10]})"
            )

    @staticmethod
    def _verify_manifest(snapshot: BootstrapSnapshot) -> None:
        """Vérifie le hash de manifest + chaque sha256 par-fichier. Fail-closed
        au premier écart, AVANT tout write."""
        manifest = snapshot.manifest
        recomputed = manifest_content_hash(manifest)
        if recomputed != manifest.manifest_sha256:
            raise BootstrapError(
                "invalid manifest_sha256: manifest was modified or truncated"
            )
        for entry in manifest.entries:
            if entry.path not in snapshot.files:
                raise BootstrapError(
                    f"missing file in snapshot: {entry.path!r}"
                )
            raw = snapshot.files[entry.path].encode("utf-8")
            digest = _sha256_bytes(raw)
            if digest != entry.sha256:
                raise BootstrapError(
                    f"invalid checksum for {entry.path!r}: "
                    f"{digest} != {entry.sha256}"
                )
        # Refuser un fichier en trop non couvert par le manifest (intégrité
        # bidirectionnelle).
        manifest_paths = {e.path for e in manifest.entries}
        extra = set(snapshot.files) - manifest_paths
        if extra:
            raise BootstrapError(
                f"files outside manifest in snapshot: {sorted(extra)[:10]}"
            )


# =============================================================================
# Surface de statut read-only (données pour #10)
# =============================================================================


async def hive_status(storage: StorageService, space_id: str) -> dict[str, Any]:
    """
    Compose une vue read-only de la santé Hivemind d'un space pour #10.

    Ne mute rien. Renvoie ``hive_status`` (disabled/healthy/resync_required/
    unsafe), peers actifs, membership_epoch, protocol_version, bank_version,
    node_status et reason.
    """
    store = HivemindStateStore(storage=storage, space_id=space_id)
    ctx = await resolve_hive_context(storage, space_id)
    health = await store.get_node_status()
    pointer = await store.get_bank_version_pointer()
    term_state = await store.get_term()

    peers = [
        {
            "node_id": m.node_id,
            "display_name": m.display_name,
            "status": m.status,
            "endpoint": m.endpoint,
        }
        for m in (ctx.membership.members if ctx.membership is not None else [])
    ]

    return {
        "space_id": space_id,
        "hive_status": ctx.node_status.value,
        "is_hive": ctx.is_hive,
        "protocol_version": layout.PROTOCOL_VERSION,
        "membership_epoch": ctx.membership.epoch if ctx.membership is not None else None,
        "peers": peers,
        "expected_ack_node_ids": expected_ack_node_ids(ctx.membership),
        "term": term_state.term if term_state is not None else None,
        "bank_version": pointer.bank_version if pointer is not None else None,
        "commit_id": pointer.commit_id if pointer is not None else None,
        "node_status": health.status if health is not None else ctx.node_status.value,
        "reason": health.reason if health is not None else "",
    }
