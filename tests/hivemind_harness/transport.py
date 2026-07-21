# -*- coding: utf-8 -*-
"""
Transport réseau falsifiable et déterministe (issue #11).

``FaultyTransport`` implémente le ``PeerTransport`` Protocol publié
(``peer.py``) afin de rester drop-in pour le vrai transport et pour les
travaux quorum/hub futurs, sans changer la sémantique V1.

Contrairement à ``InMemoryPeerTransport`` (qui ne sait que livrer ou échouer
en TRANSPORT_UNAVAILABLE), ce transport sépare l'ENVOI (``send`` met le
message dans la boîte d'expédition du pair) de la LIVRAISON (``deliver_next`` /
``deliver_all`` tirent les messages et les passent au receiver). C'est ce
découplage pull-based qui rend l'injection de drop/reorder/duplicate/delay
explicite et lisible — le test contrôle l'ordre exact, jamais le hasard.

Déterminisme :

- aucune source d'aléa par défaut ; un ``seed`` optionnel n'est utilisé que
  par les helpers de réordonnancement aléatoire bornés (property tests) ;
- l'itération sur les boîtes se fait dans l'ordre d'insertion (FIFO réseau) ;
- une partition est un split de composant explicite : l'ensemble isolé est un
  côté de la frontière, seul le trafic qui la croise est coupé (XOR).
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from live_mem.core.hivemind import (
    Member,
    PeerChannelError,
    PeerDeliveryResult,
    PeerErrorCode,
    SignedPeerEvent,
)


@dataclass
class _PendingMessage:
    """Un message en transit, identifié pour drop/duplicate/reorder ciblés."""

    seq: int
    peer_node_id: str
    message: SignedPeerEvent


# Type d'un livreur : prend un node_id destinataire + le message signé, le
# remet au receiver, et retourne un résultat opaque (souvent PeerReceiveResult
# ou une exception remontée). Enregistré par le ClusterHarness.
DeliveryHandler = Callable[[str, SignedPeerEvent], Awaitable[object]]


@dataclass
class FaultyTransport:
    """
    Transport déterministe avec injection explicite de fautes.

    Cycle de vie d'un message :

    1. ``send(peer, message)`` (appelé par ``HivemindPeerChannel.send``) place
       le message en *file de transit* du pair, SANS le livrer.
    2. Le test orchestre la livraison via ``deliver_next`` / ``deliver_all`` /
       ``drop`` / ``duplicate`` / ``reorder``, ce qui invoque le
       ``delivery_handler`` enregistré pour remettre le message au receiver.

    Deux modèles de coupure réseau, indépendants, font lever
    ``TRANSPORT_UNAVAILABLE`` à ``send`` (fail-closed comme le vrai transport)
    au lieu d'enfiler le message :

    - ``unavailable_peers`` : un pair destinataire explicitement injoignable
      (indisponibilité ciblée, indépendante des partitions) ;
    - ``partition`` : un SPLIT de composant. Seul le trafic croisant la
      frontière entre le composant isolé et le reste du cluster est coupé (dans
      les deux sens) ; le trafic intra-composant passe.
    """

    unavailable_peers: set[str] = field(default_factory=set)
    seed: int | None = None
    # File de transit par destinataire (ordre d'insertion = ordre réseau).
    inboxes: dict[str, list[_PendingMessage]] = field(default_factory=dict)
    # Composant isolé par une partition. La coupure modélise un SPLIT de
    # composant : ``_partitioned`` est UN côté de la frontière, le reste du
    # cluster est l'autre. Seul le trafic CROISANT la frontière est coupé (dans
    # les deux sens) ; le trafic INTRA-composant (deux nœuds du même côté) passe.
    _partitioned: set[str] = field(default_factory=set)
    # Compteurs d'observabilité pour les assertions de test.
    sent_count: int = 0
    delivered_count: int = 0
    dropped_count: int = 0
    _seq: int = 0
    _delivery_handler: DeliveryHandler | None = None
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # ─────────────────────────────────────────────────────────────────
    # Câblage harnais
    # ─────────────────────────────────────────────────────────────────

    def set_delivery_handler(self, handler: DeliveryHandler) -> None:
        """Enregistre la fonction qui remet un message au receiver."""
        self._delivery_handler = handler

    def partition(self, node_set: set[str]) -> None:
        """
        Isole ``node_set`` comme un COMPOSANT séparé du reste du cluster. La
        coupure suit la FRONTIÈRE entre les deux côtés (le composant
        ``node_set`` et son complément), dans LES DEUX SENS :

        - le trafic CROISANT la frontière (un endpoint dans ``node_set``,
          l'autre dehors) est coupé en entrée comme en sortie ;
        - le trafic INTRA-composant (les deux endpoints du MÊME côté, tous deux
          dans ``node_set`` OU tous deux dehors) continue de passer.

        C'est un split de composant, pas une isolation d'individus :
        ``partition({A, B})`` coupe {A, B} du reste mais laisse A↔B circuler.
        Rétro-compatible avec l'isolation d'un nœud unique : ``partition({X})``
        coupe X↔autre dans les deux sens (frontière croisée) tout en laissant le
        reste du cluster communiquer. Idempotent.

        ``unavailable_peers`` n'est PAS muté ici : la coupure de partition est
        portée par la garde XOR sur ``_partitioned`` dans ``send`` (sinon, marquer
        les deux endpoints d'un même composant ``unavailable`` bloquerait à tort
        le trafic intra-composant). ``unavailable_peers`` reste réservé à
        l'indisponibilité explicite d'un pair indépendamment des partitions.
        """
        self._partitioned |= set(node_set)

    def heal_partition(self) -> None:
        self._partitioned.clear()

    # ─────────────────────────────────────────────────────────────────
    # PeerTransport Protocol : send
    # ─────────────────────────────────────────────────────────────────

    async def send(
        self, peer: Member, message: SignedPeerEvent
    ) -> PeerDeliveryResult:
        # Partition = split de COMPOSANT : refuser UNIQUEMENT si la source et le
        # destinataire sont sur des côtés OPPOSÉS de la frontière (XOR). Le
        # trafic intra-composant (les deux dans ``_partitioned`` OU les deux
        # dehors) passe ; seul le trafic croisant la frontière est coupé, dans
        # les deux sens. Rétro-compatible avec l'isolation d'un nœud unique
        # (``partition({X})`` : X↔autre est croisant -> coupé ; autre↔autre2 passe).
        source = message.signer_node_id
        if (source in self._partitioned) != (peer.node_id in self._partitioned):
            raise PeerChannelError(
                PeerErrorCode.TRANSPORT_UNAVAILABLE,
                f"transport coupé par partition (source={source!r}, "
                f"dest={peer.node_id!r}) — endpoints de part et d'autre de la "
                f"frontière de partition",
                {"peer_node_id": peer.node_id, "source_node_id": source},
            )
        if peer.node_id in self.unavailable_peers:
            raise PeerChannelError(
                PeerErrorCode.TRANSPORT_UNAVAILABLE,
                f"transport indisponible pour peer {peer.node_id!r}",
                {"peer_node_id": peer.node_id},
            )
        self._seq += 1
        self.sent_count += 1
        self.inboxes.setdefault(peer.node_id, []).append(
            _PendingMessage(seq=self._seq, peer_node_id=peer.node_id, message=message)
        )
        return PeerDeliveryResult(peer_node_id=peer.node_id, event_id=message.event_id)

    # ─────────────────────────────────────────────────────────────────
    # Introspection de la file de transit
    # ─────────────────────────────────────────────────────────────────

    def pending(self, peer_node_id: str) -> list[SignedPeerEvent]:
        """Messages encore en transit vers ``peer_node_id`` (copie lecture)."""
        return [p.message for p in self.inboxes.get(peer_node_id, [])]

    def total_pending(self) -> int:
        return sum(len(v) for v in self.inboxes.values())

    # ─────────────────────────────────────────────────────────────────
    # Livraison explicite (pull-based)
    # ─────────────────────────────────────────────────────────────────

    async def deliver_next(self, peer_node_id: str) -> object:
        """
        Livre le PROCHAIN message en transit vers ``peer_node_id`` (FIFO).
        Retourne le résultat du handler (ou propage l'exception du receiver).
        """
        queue = self.inboxes.get(peer_node_id)
        if not queue:
            raise AssertionError(
                f"deliver_next: aucune file de transit pour {peer_node_id!r}"
            )
        pending = queue.pop(0)
        return await self._handle(pending)

    async def deliver_all(self, peer_node_id: str | None = None) -> list[object]:
        """
        Livre tous les messages en transit (pour un pair, ou pour tous si
        ``peer_node_id`` est ``None``), dans l'ordre FIFO d'insertion.

        Le tri des destinataires est déterministe (clés triées) pour que la
        livraison globale soit reproductible.
        """
        results: list[object] = []
        targets = (
            [peer_node_id]
            if peer_node_id is not None
            else sorted(self.inboxes.keys())
        )
        for target in targets:
            queue = self.inboxes.get(target, [])
            # Borne la passe aux messages PRÉSENTS à l'entrée : un handler qui
            # (re)pousse un message ne le fait pas relivrer dans la même passe.
            budget = len(queue)
            # Livraison incrémentale : on pop chaque message DU FRONT juste
            # avant de le traiter. Si ``_handle`` lève (ex. receiver rejette un
            # term périmé / epoch erroné), l'exception se propage SANS jeter les
            # messages encore en file — ils restent pending pour une relivraison
            # ultérieure (cohérent avec ``deliver_next``). L'ancien code
            # snapshotait+vidait la file d'abord, perdant silencieusement les
            # messages situés après le mauvais et masquant les bugs de retry.
            for _ in range(budget):
                if not queue:
                    break
                pending = queue.pop(0)
                results.append(await self._handle(pending))
        return results

    def drop(self, peer_node_id: str, index: int = 0) -> SignedPeerEvent:
        """
        Jette un message en transit (perte réseau) sans le livrer. ``index``
        cible une position dans la file FIFO (0 = le plus ancien).
        """
        queue = self.inboxes.get(peer_node_id)
        if not queue or index >= len(queue):
            raise AssertionError(
                f"drop: pas de message à l'index {index} pour {peer_node_id!r}"
            )
        pending = queue.pop(index)
        self.dropped_count += 1
        return pending.message

    def duplicate(self, peer_node_id: str, index: int = 0) -> None:
        """
        Duplique un message en transit (le réseau l'a livré deux fois). Le
        clone est inséré juste après l'original ; les deux seront livrés.
        """
        queue = self.inboxes.get(peer_node_id)
        if not queue or index >= len(queue):
            raise AssertionError(
                f"duplicate: pas de message à l'index {index} pour {peer_node_id!r}"
            )
        original = queue[index]
        self._seq += 1
        clone = _PendingMessage(
            seq=self._seq,
            peer_node_id=original.peer_node_id,
            message=original.message,
        )
        queue.insert(index + 1, clone)

    def reorder(self, peer_node_id: str, order: list[int]) -> None:
        """
        Réordonne explicitement la file de transit d'un pair selon ``order``
        (permutation des index courants). Modélise un réseau qui livre dans le
        désordre. ``order`` doit être une permutation exacte des index.
        """
        queue = self.inboxes.get(peer_node_id, [])
        if sorted(order) != list(range(len(queue))):
            raise AssertionError(
                f"reorder: {order} n'est pas une permutation de "
                f"range({len(queue)}) pour {peer_node_id!r}"
            )
        self.inboxes[peer_node_id] = [queue[i] for i in order]

    def shuffle(self, peer_node_id: str) -> None:
        """Réordonnancement aléatoire BORNÉ et SEEDÉ (property tests only)."""
        queue = self.inboxes.get(peer_node_id, [])
        self._rng.shuffle(queue)

    # ─────────────────────────────────────────────────────────────────
    # Interne
    # ─────────────────────────────────────────────────────────────────

    async def _handle(self, pending: _PendingMessage) -> object:
        if self._delivery_handler is None:
            raise AssertionError(
                "FaultyTransport.delivery_handler non câblé "
                "(ClusterHarness doit appeler set_delivery_handler)"
            )
        self.delivered_count += 1
        return await self._delivery_handler(pending.peer_node_id, pending.message)
