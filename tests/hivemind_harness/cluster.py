# -*- coding: utf-8 -*-
"""
ClusterHarness — orchestrateur déterministe multi-nœuds (issue #11).

Construit N ``NodeRuntime`` (storage souverain chacun) reliés par UN seul
``FaultyTransport``, sème une membership view IDENTIQUE (epoch + clés
publiques) et un term initial identique sur chaque storage, puis expose des
primitives de pas (sign+send, deliver, advance clock).

Souveraineté S3 : chaque nœud n'a que SON ``FakeStorage`` ; aucun fait
inter-nœud ne circule en dehors de ``channel.receive`` (déclenché par la
livraison du transport). C'est ce qui rend les fautes réseau observables sur
l'état.

Déterminisme : un seul thread, pas d'``asyncio`` concurrent, le test pilote la
livraison. Toute collection ordonnée est triée explicitement (les nœuds sont
itérés par ``node_id`` trié).
"""

from __future__ import annotations

from collections.abc import Iterable

from live_mem.core.hivemind import (
    EventEnvelope,
    HivemindStateStore,
    Member,
    MembershipView,
    NodeIdentity,
    PeerReceiveResult,
    SignedPeerEvent,
    generate_peer_keypair,
)

from .clock import DeterministicClock
from .node import NodeRuntime
from .policy import AckPolicy, AllAckPolicy
from .transport import FaultyTransport

# Réutilise le FakeStorage des tests #3 pour la parité de comportement.
from tests.test_hivemind_state import FakeStorage


class ClusterHarness:
    """
    Un cluster Hivemind simulé, déterministe et falsifiable.

    Construire avec ``await ClusterHarness.create(...)`` (l'init nécessite des
    writes async pour semer membership/term sur chaque storage).
    """

    def __init__(
        self,
        *,
        space_id: str,
        clock: DeterministicClock,
        transport: FaultyTransport,
        ack_policy: AckPolicy,
    ) -> None:
        self.space_id = space_id
        self.clock = clock
        self.transport = transport
        self.ack_policy = ack_policy
        self.nodes: dict[str, NodeRuntime] = {}
        self.epoch: int = 0
        self.term: int = 0

    # ─────────────────────────────────────────────────────────────────
    # Construction
    # ─────────────────────────────────────────────────────────────────

    @classmethod
    async def create(
        cls,
        *,
        node_ids: Iterable[str],
        space_id: str = "alpha",
        epoch: int = 1,
        term: int = 1,
        clock: DeterministicClock | None = None,
        transport: FaultyTransport | None = None,
        ack_policy: AckPolicy | None = None,
        replay_window_seconds: int = 300,
    ) -> "ClusterHarness":
        clock = clock or DeterministicClock()
        transport = transport or FaultyTransport()
        ack_policy = ack_policy or AllAckPolicy()
        node_ids = list(node_ids)

        harness = cls(
            space_id=space_id,
            clock=clock,
            transport=transport,
            ack_policy=ack_policy,
        )
        harness.epoch = epoch
        harness.term = term

        # 1) keypair par nœud (déterminisme : l'aléa Ed25519 n'affecte pas la
        # logique protocole, seulement les signatures, qui sont vérifiées).
        keypairs = {nid: generate_peer_keypair() for nid in node_ids}

        # 2) membership view identique partagée par tous (clés publiques).
        members = [
            Member(
                node_id=nid,
                display_name=nid,
                endpoint=f"memory://{nid}",
                public_key=keypairs[nid].public_key,
            )
            for nid in node_ids
        ]
        view = MembershipView(epoch=epoch, members=members)

        # 3) un NodeRuntime par nœud, chacun avec son storage souverain ; on
        # sème identité + membership + term sur CHAQUE storage.
        for nid in node_ids:
            node = NodeRuntime(
                node_id=nid,
                space_id=space_id,
                keypair=keypairs[nid],
                clock=clock,
                transport=transport,
                replay_window_seconds=replay_window_seconds,
            )
            await node.store.set_node_identity(
                NodeIdentity(
                    node_id=nid,
                    display_name=nid,
                    public_key=keypairs[nid].public_key,
                )
            )
            await node.store.set_membership(view.model_copy(deep=True))
            await node.store.bump_term(term, updated_by_node_id=nid)
            harness.nodes[nid] = node

        # 4) câbler la livraison du transport vers les receivers.
        transport.set_delivery_handler(harness._deliver_to)
        return harness

    # ─────────────────────────────────────────────────────────────────
    # Accès
    # ─────────────────────────────────────────────────────────────────

    def node(self, node_id: str) -> NodeRuntime:
        return self.nodes[node_id]

    def node_ids(self) -> list[str]:
        return sorted(self.nodes.keys())

    def peers_of(self, node_id: str) -> list[str]:
        """Tous les autres nœuds (full-mesh), triés."""
        return [n for n in self.node_ids() if n != node_id]

    async def membership(self) -> MembershipView:
        """Membership courante (lue depuis un nœud ; identique partout)."""
        view = await self.nodes[self.node_ids()[0]].store.get_membership()
        assert view is not None
        return view

    # ─────────────────────────────────────────────────────────────────
    # Primitives de pas
    # ─────────────────────────────────────────────────────────────────

    async def sign(self, origin: str, event: EventEnvelope) -> SignedPeerEvent:
        """Signe un event avec la clé du nœud d'origine, à l'instant logique."""
        node = self.nodes[origin]
        return await node.channel.sign_event(event, signed_at=self.clock.iso())

    async def send_to(
        self, origin: str, peer_id: str, event: EventEnvelope
    ) -> None:
        """
        Met en transit UN event signé vers ``peer_id``. La signature porte le
        ``signed_at`` LOGIQUE (sinon ``channel.send`` re-signe avec l'horloge
        murale via ``_now_iso`` et casse le déterminisme / la fenêtre de rejeu).
        """
        message = await self.sign(origin, event)
        view = await self.membership()
        member = next(m for m in view.members if m.node_id == peer_id)
        await self.transport.send(member, message)

    async def broadcast(self, origin: str, event: EventEnvelope) -> None:
        """
        Diffuse un event signé vers TOUS les pairs (full-mesh), via le
        transport. Les messages restent EN TRANSIT jusqu'à livraison explicite.
        """
        for peer_id in self.peers_of(origin):
            await self.send_to(origin, peer_id, event)

    async def _deliver_to(
        self, peer_node_id: str, message: SignedPeerEvent
    ) -> PeerReceiveResult:
        """Handler de livraison : remet le message au channel du destinataire."""
        node = self.nodes[peer_node_id]
        if not node.alive or node.channel is None:
            raise AssertionError(
                f"livraison à un nœud crashé {peer_node_id!r} (bug de scénario)"
            )
        return await node.channel.receive(message)

    def tick(self, **kwargs: int) -> None:
        """Avance l'horloge logique partagée (lease/replay window)."""
        self.clock.tick(**kwargs)

    # ─────────────────────────────────────────────────────────────────
    # Helpers ACK (consomment le seam de politique)
    # ─────────────────────────────────────────────────────────────────

    async def received_ack_set(self, event_id: str) -> set[str]:
        """
        Ensemble des ``ack_by_node_id`` persistés pour ``event_id`` agrégés sur
        TOUS les nœuds (union cross-nœud).

        On retourne l'IDENTITÉ des ACKers, pas un simple compte : la politique
        all-ACK valide par sur-ensemble (chaque actif a ACKé), donc un ACK d'un
        nœud non-actif ne peut pas combler l'absence d'un actif.
        """
        seen: set[str] = set()
        for nid in self.node_ids():
            store = self.nodes[nid].store
            for ack in await store.list_acks(event_id):
                seen.add(ack.ack_by_node_id)
        return seen

    async def received_acks(self, event_id: str) -> int:
        """Cardinalité de l'ensemble des ACKers distincts (compat introspection)."""
        return len(await self.received_ack_set(event_id))

    async def all_acked(self, event_id: str, *, on_node: str | None = None) -> bool:
        """
        True si la politique d'ACK est satisfaite pour ``event_id``.

        ``on_node`` cible le storage du holder qui agrège les ACK (chemin V1
        réel) : on valide alors les ACK reçus PAR LE HOLDER contre la membership
        VUE PAR LE HOLDER. C'est essentiel dès que les vues de membership
        divergent (resync / éviction partielle) : valider les ACK du holder
        contre l'ensemble actif d'un AUTRE nœud autoriserait/bloquerait un grant
        à tort (le holder décide sur SA propre vue, cf. HIVEMIND.md §6.1). Si le
        holder n'a pas de membership locale, on retombe sur la membership du
        cluster.

        Sinon (``on_node=None``), on prend l'union cross-nœud des ACK contre la
        membership du cluster. Dans les deux cas on passe l'ENSEMBLE des
        ``ack_by_node_id`` à la politique (validation par identité), jamais un
        simple compteur.
        """
        if on_node is not None:
            holder_store = self.nodes[on_node].store
            received = {
                ack.ack_by_node_id
                for ack in await holder_store.list_acks(event_id)
            }
            # Vue de membership DU HOLDER (peut diverger d'un autre nœud).
            membership = await holder_store.get_membership()
            if membership is None:
                membership = await self.membership()
        else:
            received = await self.received_ack_set(event_id)
            membership = await self.membership()
        return self.ack_policy.is_satisfied(received=received, membership=membership)
