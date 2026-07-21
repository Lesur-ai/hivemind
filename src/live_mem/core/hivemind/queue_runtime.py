# -*- coding: utf-8 -*-
"""
Runtime de la queue durable distribuée all-ACK Hivemind (issue #6 / ADR-0009).

Ce module pose le **comparateur unique** de la queue partagée et la surface
async qui le câble au ``HivemindStateStore``. C'est le seul propriétaire de
l'ordre total des entrées de queue (ADR-0009 §Decision Outcome) : #7 (lease)
n'invente PAS son propre ordre, il appelle ``select_head`` / ``QueueRuntime.head``.

Deux couches :

- **Couche pure** (``queue_order_key`` / ``active_requester_ids`` /
  ``select_head`` / ``detect_seq_collisions`` / ``SeqCollision``) : des fonctions
  sans I/O, sans wall-clock, sans mutation, qui n'opèrent que sur des listes
  d'entrées déjà chargées. C'est elle que #7 importe verbatim et c'est elle qui
  rend *prouvable* qu'une corruption ne peut pas être contournée (elle ne
  contient aucun ``try/except``).
- **Couche async** (``QueueRuntime``) : un wrapper mince autour du store. Elle
  charge l'état (``list_queue`` / ``list_acks``) et délègue à la couche pure.
  La garantie fail-closed vit ENTIÈREMENT dans le fait de NE PAS rattraper
  ``CorruptedStateError`` au site d'appel du store — zéro ``try/except`` dans
  tout ce module.

Invariants protocole portés (ADR-0009) :

- Ordre total = tuple ascendant ``(sequence, membership_epoch,
  requester_node_id, event_id)``. Le prose de l'ADR nomme le 3ᵉ terme
  ``origin_node_id`` ; le champ réellement persisté sur ``QueueEntry`` est
  ``requester_node_id`` (réconcilié par la note de ratification P5-0) — on
  l'utilise tel quel, on n'ajoute AUCUN champ (modèles ``extra="forbid"``).
- HEAD = le minimum sous cet ordre qui est encore ``PENDING`` ET dont le
  demandeur est un membre ``ACTIVE`` de l'``membership_epoch`` courant.
- all-ACK = IDENTITÉ d'ensemble sur les membres ACTIVE (chaque node_id présent),
  PAS un compte. Un peer actif qui n'ACK pas laisse l'op visiblement bloquée
  (pending), jamais avancée/réordonnée/droppée.
- ``sequence`` reste alloué best-effort côté caller (pas d'allocateur atomique,
  S3 n'a pas de CAS) ; une collision de seq est ordonnée déterministiquement
  (pas de split-brain) ET surfacée comme anomalie détectable — JAMAIS
  silencieusement coalescée.
- Fail-closed : une entrée corrompue/illisible bloque la sélection de head
  (``CorruptedStateError`` propage), jamais skippée/devinée.
- Idempotence event_id : ré-enqueue / ré-ACK FIDÈLE (mêmes champs d'identité)
  du même event_id est un no-op ; un ré-enqueue DIVERGENT (même event_id,
  payload logique différent) est un rejet protocole (``QueueReplayConflictError``,
  fail-closed), jamais un succès silencieux.

Ce module n'importe AUCUNE mémoire longue / graph (pas de ``graph_push``), ne
touche PAS au consolidateur en mémoire des spaces non-Hivemind, et n'utilise
aucun timer / wall-clock.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .lifecycle import expected_ack_node_ids
from .models import (
    Ack,
    CorruptedStateError,
    MembershipView,
    QueueEntry,
    QueueEntryStatus,
    QueueReplayConflictError,
)
from .state import HivemindStateStore


# =============================================================================
# Couche pure — le comparateur ADR-0009 (importé verbatim par #7)
# =============================================================================


def queue_order_key(entry: QueueEntry) -> tuple[int, int, str, str]:
    """
    Clé d'ordre total ADR-0009 sur une entrée de queue.

    Tuple ascendant ``(sequence, membership_epoch, requester_node_id,
    event_id)`` — uniquement des champs immuables déjà persistés, aucun
    wall-clock. Les quatre champs sont non-optionnels et totalement
    ordonnables (jamais ``None``), donc l'ordre est total.

    NB : le prose ADR-0009 nomme le 3ᵉ terme ``origin_node_id`` ; le champ
    réellement présent sur ``QueueEntry`` est ``requester_node_id``.
    """
    return (
        entry.sequence,
        entry.membership_epoch,
        entry.requester_node_id,
        entry.event_id,
    )


def active_requester_ids(membership: MembershipView | None) -> frozenset[str]:
    """
    L'ensemble ACTIVE unique — réutilise ``lifecycle.expected_ack_node_ids``.

    UNE seule source de vérité pour l'éligibilité du head ET l'all-ACK : on ne
    re-filtre PAS la membership ici et on n'imite PAS la policy de test
    (``AllAckPolicy``). C'est le même ensemble que celui dont un ACK est attendu.
    """
    return frozenset(expected_ack_node_ids(membership))


def select_head(
    entries: list[QueueEntry],
    membership: MembershipView | None,
) -> QueueEntry | None:
    """
    Le head : minimum sous ``queue_order_key`` parmi les entrées qui sont À LA
    FOIS ``status == PENDING`` ET dont le ``requester_node_id`` est dans
    l'ensemble ACTIVE — et qui sont l'entrée CANONIQUE de leur ``event_id``.

    UNICITÉ LOGIQUE PAR event_id : un même ``event_id`` peut occuper PLUSIEURS
    objets durables à des seq distincts (rejeu fidèle / course S3 passée, pas de
    CAS — cf. ``submit``). Seule l'entrée CANONIQUE (min sous ``queue_order_key``
    PARMI TOUTES les entrées de cet ``event_id``, quel que soit leur status) est
    head-éligible. Sans ce filtre, un duplicata NON-canonique resté ``PENDING``
    deviendrait un head INDÉPENDANT après que le canonique a été
    granted/cancelled — un même événement logique accordé/annulé pourrait alors
    redevenir head (double-grant). Le filtre est read-only : les duplicatas ne
    sont JAMAIS coalescés ni supprimés (ils restent surfacés par
    ``detect_event_id_duplicates`` / ``queue_anomalies`` à #10 / recovery).

    Fonction PURE : aucune I/O, aucune mutation, aucun effet de bord de tri,
    AUCUN ``try/except``. Indépendante de l'ordre de la liste d'entrée (``min``,
    jamais ``entries[0]``) — deux peers partant du même snapshot dans un ordre
    d'insertion différent calculent le même head ET la même entrée canonique
    par event_id.
    """
    active = active_requester_ids(membership)
    canonical = _canonical_entries_by_event_id(entries)
    eligible = [
        e
        for e in canonical
        if e.status == QueueEntryStatus.PENDING.value
        and e.requester_node_id in active
    ]
    if not eligible:
        return None
    return min(eligible, key=queue_order_key)


def _canonical_entries_by_event_id(
    entries: list[QueueEntry],
) -> list[QueueEntry]:
    """
    Pour chaque ``event_id``, l'entrée CANONIQUE = le min sous
    ``queue_order_key`` PARMI TOUTES ses entrées (tous status confondus).

    Pur, read-only, déterministe (``min``, jamais ``entries[0]``). Un
    ``event_id`` unique rend l'entrée elle-même ; un ``event_id`` dupliqué à
    plusieurs seq rend la seule entrée de plus bas ordre. Les duplicatas
    non-canoniques sont EXCLUS de l'éligibilité head mais JAMAIS supprimés du
    store.
    """
    by_event: dict[str, list[QueueEntry]] = {}
    for entry in entries:
        by_event.setdefault(entry.event_id, []).append(entry)
    return [min(group, key=queue_order_key) for group in by_event.values()]


@dataclass(frozen=True)
class SeqCollision:
    """
    Anomalie de queue : une ``sequence`` réutilisée par >= 2 ``event_id``
    distincts (deux peers ayant alloué le même seq best-effort).

    Surfacée pour l'observabilité (#10 / P5-4) mais l'ordre reste déterministe
    (cf. ``queue_order_key``) : pas de split-brain, jamais coalescée.
    """

    sequence: int
    event_ids: tuple[str, ...]  # event_ids colluants distincts, triés
    requester_node_ids: tuple[str, ...]  # demandeurs distincts à ce seq, triés


def detect_seq_collisions(entries: list[QueueEntry]) -> list[SeqCollision]:
    """
    Groupe les entrées ``PENDING`` par ``sequence`` ; une collision = une
    sequence tenue par >= 2 ``event_id`` DISTINCTS (le même event_id deux fois
    = doublon idempotent, PAS une collision).

    Read-only ; ne coalesce JAMAIS ; sortie triée (par ``sequence``, ids triés
    à l'intérieur). ``[]`` quand c'est propre.
    """
    pending = [
        e for e in entries if e.status == QueueEntryStatus.PENDING.value
    ]
    by_seq: dict[int, list[QueueEntry]] = {}
    for entry in pending:
        by_seq.setdefault(entry.sequence, []).append(entry)

    collisions: list[SeqCollision] = []
    for sequence, group in by_seq.items():
        event_ids = {e.event_id for e in group}
        if len(event_ids) < 2:
            continue
        requester_node_ids = {e.requester_node_id for e in group}
        collisions.append(
            SeqCollision(
                sequence=sequence,
                event_ids=tuple(sorted(event_ids)),
                requester_node_ids=tuple(sorted(requester_node_ids)),
            )
        )
    collisions.sort(key=lambda c: c.sequence)
    return collisions


@dataclass(frozen=True)
class DuplicateEventId:
    """
    Anomalie de queue : un même ``event_id`` occupe >= 2 objets durables à des
    ``sequence`` DISTINCTES — quel que soit leur status (rejeu fidèle / course S3
    passée, pas de CAS — la clé store ``{sequence}_{event_id}`` autorise plusieurs
    objets pour un seul événement logique).

    DISTINCTE de ``SeqCollision`` : celle-ci groupe par ``event_id`` (un id à
    plusieurs seq) ; ``SeqCollision`` groupe par ``sequence`` (un seq à plusieurs
    ids). Surfacée pour l'observabilité / recovery (#10 / P5-4) : un duplicata
    non-canonique resté ``PENDING`` pourrait sinon devenir un head indépendant
    après que le canonique a été granted/cancelled. ``select_head`` neutralise
    déjà cette liveness (seule l'entrée canonique est head-éligible), mais l'état
    résiduel reste SURFACÉ — y compris APRÈS que le canonique a quitté ``PENDING``
    (la détection groupe tous status, cf. ``detect_event_id_duplicates``) —
    JAMAIS coalescé ni auto-supprimé par le runtime (S3 n'a pas de transaction
    multi-clé ; la suppression relève de recovery).
    """

    event_id: str
    sequences: tuple[int, ...]  # seq distinctes du même event_id, triées
    requester_node_ids: tuple[str, ...]  # demandeurs distincts, triés


def detect_event_id_duplicates(
    entries: list[QueueEntry],
) -> list[DuplicateEventId]:
    """
    Groupe les entrées par ``event_id`` — TOUS STATUS CONFONDUS — ; un duplicata
    = un ``event_id`` tenu par >= 2 ``sequence`` DISTINCTES (deux objets durables
    ``{sequence}_{event_id}`` pour un même événement logique).

    TOUS STATUS, PAS seulement ``PENDING`` (asymétrie DÉLIBÉRÉE avec
    ``detect_seq_collisions``, justifiée ci-dessous) : un ``event_id`` dupliqué
    est de la corruption RÉSIDUELLE (un seul événement logique, plusieurs objets
    durables nés d'une course S3 sans CAS) qui PERSISTE jusqu'à ce que recovery
    la nettoie. Filtrer sur ``PENDING`` la rendrait INVISIBLE dès que l'entrée
    canonique passe ``GRANTED``/``CANCELLED`` : il ne resterait que le duplicata
    non-canonique ``PENDING`` (``len(sequences) < 2`` -> aucune anomalie), et
    l'état résiduel disparaîtrait de l'observabilité EXACTEMENT après la
    transition que cette détection est censée surveiller. On groupe donc sur
    toutes les entrées pour que le duplicata reste surfacé tant que les deux
    objets durables coexistent. (Un ``event_id`` non dupliqué — un seul objet,
    quel que soit son status — n'est JAMAIS faux-positif : ``len(sequences) ==
    1``.)

    ``detect_seq_collisions`` reste, lui, restreint à ``PENDING`` : une collision
    de seq groupe >= 2 ``event_id`` DISTINCTS (deux événements LÉGITIMES ayant
    alloué le même seq best-effort) ; chacun devient head à son tour sous l'ordre
    total puis est consommé — il n'y a pas d'état résiduel à nettoyer, donc
    surfacer après consommation serait du bruit. Ici au contraire l'événement
    est UNIQUE et le duplicata résiduel doit rester visible pour recovery.

    Read-only ; ne coalesce ni ne supprime JAMAIS ; sortie triée (par
    ``event_id``, seq/ids triés à l'intérieur). ``[]`` quand c'est propre.
    """
    by_event: dict[str, list[QueueEntry]] = {}
    for entry in entries:
        by_event.setdefault(entry.event_id, []).append(entry)

    duplicates: list[DuplicateEventId] = []
    for event_id, group in by_event.items():
        sequences = {e.sequence for e in group}
        if len(sequences) < 2:
            continue
        requester_node_ids = {e.requester_node_id for e in group}
        duplicates.append(
            DuplicateEventId(
                event_id=event_id,
                sequences=tuple(sorted(sequences)),
                requester_node_ids=tuple(sorted(requester_node_ids)),
            )
        )
    duplicates.sort(key=lambda d: d.event_id)
    return duplicates


# =============================================================================
# Couche async — surface store-facing (issue #6)
# =============================================================================


# Sérialisation de la section critique de ``submit`` par space.
#
# ``submit`` n'est idempotent sur ``event_id`` que si le SCAN d'existence et
# l'écriture (allocate_sequence + enqueue) sont atomiques l'un par rapport à
# l'autre : sans ça, deux ``submit`` concurrents du même ``event_id`` peuvent
# tous deux scanner « absent », tous deux allouer un seq best-effort distinct
# (``max(seq)+1``), et écrire DEUX objets store ``{seq}_{event_id}`` pour une
# seule requête logique (la clé store est ``sequence + event_id`` — cf.
# ``state.queue_entry_key``). Un verrou par-space referme cette fenêtre :
# le 2ᵉ ``submit`` attend, RELIT la queue sous le verrou, voit l'entrée du 1ᵉ
# et la RETOURNE au lieu d'en écrire une seconde.
#
# Ce verrou ne sérialise QUE le plan local d'un même process/boucle ; il ne
# remplace pas une coordination distribuée (S3 n'a pas de CAS) — l'ordre total
# distribué reste porté par ``queue_order_key`` et une collision de seq
# cross-node reste détectée par ``detect_seq_collisions``, jamais coalescée.
#
# space_id -> (event_loop, lock). Le verrou est lié à la boucle courante et
# recréé si la boucle change (un ``asyncio.Lock`` est attaché à sa boucle ;
# le réutiliser depuis une autre lèverait). Même posture que
# ``lifecycle._membership_lock`` — mais ``queue_runtime`` possède son PROPRE
# dict (il n'importe ni ``lifecycle`` ni ``lease_runtime`` pour ça).
_SUBMIT_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _submit_lock(space_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _SUBMIT_LOCKS.get(space_id)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _SUBMIT_LOCKS[space_id] = (loop, lock)
        return lock
    return entry[1]


class QueueRuntime:
    """
    Wrapper async mince autour du ``HivemindStateStore`` pour la queue partagée.

    Ne réimplémente AUCUN stockage : il réutilise les primitives du store
    (``enqueue`` / ``list_queue`` / ``update_queue_entry_status`` /
    ``record_ack`` / ``list_acks``) et délègue tout l'ordre à la couche pure
    ci-dessus.

    Fail-closed : aucune méthode ne rattrape ``CorruptedStateError`` — une
    entrée de queue ou un ACK corrompu propage et BLOQUE (jamais skippé,
    jamais compté, jamais deviné).
    """

    def __init__(self, store: HivemindStateStore, space_id: str) -> None:
        if space_id != store.space_id:
            raise ValueError(
                f"QueueRuntime space_id={space_id!r} != "
                f"store.space_id={store.space_id!r}"
            )
        self._store = store
        self._space_id = space_id

    @property
    def space_id(self) -> str:
        return self._space_id

    async def allocate_sequence(self) -> int:
        """
        ``max(seq existants) + 1``, ou ``0`` si la queue est vide.

        C'est un HINT, pas un lock : S3 n'a pas de CAS et LIST est
        éventuellement cohérent, donc deux peers peuvent choisir la même
        valeur. La totalité de l'ordre est garantie par la queue de
        ``queue_order_key`` (membership_epoch, requester_node_id, event_id),
        PAS par l'unicité du seq. Empty -> 0 respecte le validator
        ``sequence >= 0``.
        """
        entries = await self._store.list_queue()
        return max((e.sequence for e in entries), default=-1) + 1

    async def submit(
        self,
        *,
        event_id: str,
        requester_node_id: str,
        term: int,
        membership_epoch: int,
        bank_version: int = -1,
        request_id: str = "",
        sequence: int | None = None,
    ) -> QueueEntry:
        """
        Construit une ``QueueEntry`` ``PENDING`` et l'``enqueue()``.

        ``sequence=None`` -> ``allocate_sequence()`` (hint best-effort).

        Idempotent sur ``event_id`` (PAS sur la clé store ``(sequence,
        event_id)``) : on scanne d'abord ``list_queue()`` et on collecte
        TOUTES les entrées de cet ``event_id`` — pas seulement la première.
        La clé store étant ``{sequence}_{event_id}``, un même ``event_id``
        logique peut DÉJÀ occuper PLUSIEURS objets durables à des seq
        différents (retry / course distribuée ; S3 n'a pas de CAS). S'arrêter
        au 1ᵉ match laisserait une entrée à seq bas et identité FIDÈLE MASQUER
        une entrée à seq plus haut et identité DIVERGENTE. On compare donc
        l'identité logique ``(requester_node_id, term, membership_epoch,
        bank_version)`` de l'incoming ET de toutes les entrées entre elles :

        - identité IDENTIQUE pour TOUTES les entrées (et l'incoming) -> rejeu
          fidèle, on RETOURNE l'entrée CANONIQUE (le min sous
          ``queue_order_key``, i.e. le seq le plus bas) sans réécrire
          (idempotence + convergence déterministe entre callers concurrents).
          Sans ce garde, ré-soumettre le même ``event_id`` avec
          ``sequence=None`` ré-alloue ``max(seq)+1`` et écrirait un SECOND
          objet store ``{seq}_{event_id}`` pour la même requête logique (rejeu
          d'un claim de token déjà accordé/annulé). On NE devine PAS un seq fixe
          ni n'écrase le ``status`` de l'entrée existante : on rend la première
          écriture autoritative.
        - identité DIVERGENTE — soit l'incoming diverge d'une entrée persistée,
          soit DEUX entrées persistées du même ``event_id`` divergent entre
          elles — -> ``QueueReplayConflictError`` (fail-closed, AUCUNE seconde
          écriture). Un même ``event_id`` identifie UN seul événement logique :
          le ré-soumettre avec un autre demandeur / term / epoch / bank_version
          est une ERREUR PROTOCOLE, jamais un succès silencieux. Même sémantique
          que ``REPLAY_CONFLICT`` côté ``peer.py`` (même ``event_id`` + payload
          différent = rejet).

        RÉSIDUEL S3 (pas de transaction multi-clé) : ce garde DÉTECTE et bloque
        fail-closed un ``event_id`` déjà dupliqué sous des seq distincts (la
        soumission entrante n'ajoute jamais de 3ᵉ objet, et un duplicata
        divergent préexistant lève), mais il ne peut PAS supprimer atomiquement
        les objets durables déjà écrits par une course passée. La linéarisation
        reste portée par ``queue_order_key`` (entrée canonique = min) ; une
        collision résiduelle entre seq distincts reste surfacée par
        ``detect_seq_collisions``, jamais coalescée silencieusement.

        La SECTION CRITIQUE ENTIÈRE (scan d'existence + ``allocate_sequence``
        + build + ``enqueue``) est sérialisée sous un verrou par-space
        (``_submit_lock``). Sans ça, le scan et l'écriture ne sont pas
        atomiques : deux ``submit`` concurrents du MÊME ``event_id`` (retry /
        rejeu) peuvent tous deux scanner « absent », tous deux allouer un seq
        distinct et écrire DEUX objets store ``{seq}_{event_id}`` pour une
        seule requête logique. Sous le verrou, le 2ᵉ ``submit`` attend, RELIT
        la queue et voit l'entrée du 1ᵉ — il la RETOURNE au lieu d'en écrire
        une seconde. Le verrou est purement local (plan d'un même process /
        boucle) ; l'ordre total distribué reste porté par ``queue_order_key``
        et une collision de seq cross-node reste surfacée par
        ``detect_seq_collisions``, jamais coalescée.

        ``CorruptedStateError`` propage depuis ``list_queue`` (AUCUN ``except``
        ici) : une entrée corrompue BLOQUE la soumission plutôt que de risquer
        un doublon silencieux.
        """
        incoming_identity = (
            requester_node_id,
            term,
            membership_epoch,
            bank_version,
        )
        async with _submit_lock(self._space_id):
            # On scanne TOUTES les entrées du même ``event_id`` — PAS seulement
            # la première. La clé store est ``{sequence}_{event_id}`` : un même
            # ``event_id`` logique peut DÉJÀ occuper PLUSIEURS objets durables à
            # des seq différents (retry / course distribuée, pas de CAS S3). Si
            # on s'arrêtait au 1ᵉ match, une entrée à seq bas et identité FIDÈLE
            # pourrait MASQUER une seconde entrée à seq plus haut et identité
            # DIVERGENTE -> ``submit`` retournerait un succès silencieux et
            # laisserait l'entrée divergente devenir un head indépendant. On
            # collecte donc d'abord, puis on tranche fail-closed.
            matches = [
                e
                for e in await self._store.list_queue()
                if e.event_id == event_id
            ]
            if matches:
                # L'incoming PLUS toutes les entrées persistées du même
                # ``event_id`` doivent partager UNE seule identité logique. Tout
                # désaccord (incoming vs persisté, OU deux persistés entre eux)
                # est une ERREUR PROTOCOLE : même ``event_id`` == UN seul
                # événement logique. On lève AVANT toute écriture (fail-closed).
                identities = {
                    (
                        e.requester_node_id,
                        e.term,
                        e.membership_epoch,
                        e.bank_version,
                    )
                    for e in matches
                }
                if identities != {incoming_identity}:
                    raise QueueReplayConflictError(
                        f"event_id={event_id!r} présent avec une ou plusieurs "
                        f"identités divergentes : persistées={sorted(identities)} "
                        f"vs soumis={incoming_identity} "
                        f"(requester_node_id, term, membership_epoch, "
                        f"bank_version) — même event_id identifie UN seul "
                        f"événement logique. Couvre AUSSI le cas où DEUX objets "
                        f"durables {{sequence}}_{{event_id}} préexistants "
                        f"divergent entre eux (un seq bas fidèle masquerait "
                        f"sinon un seq haut divergent). ERREUR PROTOCOLE, "
                        f"fail-closed, aucune seconde écriture."
                    )
                # Toutes les entrées (et l'incoming) partagent la même identité
                # -> rejeu fidèle idempotent. On retourne l'entrée CANONIQUE :
                # le min sous ``queue_order_key`` (seq le plus bas), pour que
                # deux callers concurrents convergent déterministiquement sur la
                # MÊME entrée et qu'on n'écrive AUCUN second objet.
                return min(matches, key=queue_order_key)
            seq = (
                await self.allocate_sequence()
                if sequence is None
                else sequence
            )
            entry = QueueEntry(
                event_id=event_id,
                request_id=request_id,
                sequence=seq,
                requester_node_id=requester_node_id,
                term=term,
                membership_epoch=membership_epoch,
                bank_version=bank_version,
            )  # status défaut = PENDING
            return await self._store.enqueue(entry)

    async def head(
        self, membership: MembershipView | None
    ) -> QueueEntry | None:
        """
        ``select_head(await list_queue(), membership)``.

        ``CorruptedStateError`` propage depuis ``list_queue`` (AUCUN ``except``
        ici) — une entrée corrompue BLOQUE la sélection de head plutôt que
        d'être skippée.
        """
        return select_head(await self._store.list_queue(), membership)

    async def is_fully_acked(
        self, event_id: str, membership: MembershipView | None
    ) -> bool:
        """
        IDENTITÉ d'ensemble, pas un compte. ``expected`` = ensemble ACTIVE ;
        ``received`` = ``{a.ack_by_node_id}`` ; retourne
        ``expected.issubset(received)``.

        Un peer ACTIVE qui n'ACK pas -> ``False`` (l'op reste visiblement
        bloquée). Un ACKeur évincé/inconnu ne peut PAS se substituer à un
        membre actif manquant. ``list_acks`` propage ``CorruptedStateError``
        (un ACK corrompu bloque, jamais compté).

        FAIL-CLOSED sur un ensemble ACTIVE vide : ``membership is None`` ou une
        membership SANS aucun membre ACTIVE est de l'état critique
        incomplet/corrompu, PAS un all-ACK valide à zéro peer. Sans ce garde,
        ``expected`` serait l'ensemble vide et ``frozenset().issubset(received)``
        vaut ``True`` -> un faux « fully acked » sur état incomplet. On lève
        ``CorruptedStateError`` (même posture que ``peer.py`` qui refuse une
        membership absente) plutôt que de retourner un défaut-permissif.
        """
        expected = active_requester_ids(membership)
        if not expected:
            raise CorruptedStateError(
                f"is_fully_acked refuse un ensemble ACTIVE vide pour "
                f"event_id={event_id!r} (membership absente ou sans membre "
                f"ACTIVE : état critique incomplet, fail-closed)"
            )
        received = {
            a.ack_by_node_id for a in await self._store.list_acks(event_id)
        }
        return expected.issubset(received)

    async def record_ack(self, ack: Ack) -> Ack:
        """
        Délègue à ``store.record_ack``. Idempotent sur
        ``(event_id, ack_by_node_id)``. Ne masque PAS un ``payload_hash``
        divergent (laissé au protocole).
        """
        return await self._store.record_ack(ack)

    async def mark_granted(self, entry: QueueEntry) -> QueueEntry:
        """Passe une entrée à ``GRANTED`` (elle sort de l'éligibilité head)."""
        return await self._store.update_queue_entry_status(
            entry, QueueEntryStatus.GRANTED
        )

    async def mark_cancelled(self, entry: QueueEntry) -> QueueEntry:
        """Passe une entrée à ``CANCELLED`` (elle sort de l'éligibilité head)."""
        return await self._store.update_queue_entry_status(
            entry, QueueEntryStatus.CANCELLED
        )

    async def queue_anomalies(
        self,
    ) -> list[SeqCollision | DuplicateEventId]:
        """
        DEUX familles d'anomalies de queue, sur le MÊME snapshot ``list_queue()`` :

        - ``SeqCollision`` (``detect_seq_collisions``) : une ``sequence`` tenue
          par >= 2 ``event_id`` distincts.
        - ``DuplicateEventId`` (``detect_event_id_duplicates``) : un même
          ``event_id`` tenu par >= 2 ``sequence`` distinctes, TOUS STATUS
          CONFONDUS (rejeu fidèle / course S3 passée). Sans cette seconde
          famille — ou en la restreignant à ``PENDING`` — un duplicata
          non-canonique resté ``PENDING`` resterait SILENCIEUX dès que le
          canonique passe granted/cancelled, alors qu'il faut justement le garder
          surfacé pour recovery après cette transition.

        Read-only ; propage ``CorruptedStateError`` ; SURFACE les deux familles
        à #10 / recovery mais ne coalesce ni ne supprime JAMAIS (la résolution
        d'un duplicata résiduel relève de recovery, pas du runtime — S3 n'a pas
        de transaction multi-clé). ``select_head`` neutralise déjà la liveness
        (seule l'entrée canonique par ``event_id`` est head-éligible).
        """
        entries = await self._store.list_queue()
        anomalies: list[SeqCollision | DuplicateEventId] = []
        anomalies += detect_seq_collisions(entries)
        anomalies += detect_event_id_duplicates(entries)
        return anomalies
