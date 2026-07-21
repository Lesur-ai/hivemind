# -*- coding: utf-8 -*-
"""
NodeRuntime — un nœud Hivemind simulé (issue #11).

Un nœud = un ``FakeStorage`` (son sous-arbre S3 ``_hivemind/`` souverain) +
un ``HivemindStateStore`` (vues dérivées) + un ``HivemindPeerChannel`` (auth
des messages entrants) + une paire de clés Ed25519.

Crash / restart (faithful à la topologie S3) :

- ``crash()`` jette l'instance en mémoire (channel + tout intent non flushé)
  SANS toucher au ``FakeStorage``. Les writes S3 étant atomiques par objet
  (HIVEMIND_STATE.md §7), un crash == « un préfixe ordonné des writes a eu
  lieu » ; rien n'est à demi-écrit.
- ``restart()`` reconstruit un store + channel neufs sur le MÊME storage et
  appelle ``load_snapshot`` pour rebâtir la vue (chemin cold-start réel).
"""

from __future__ import annotations

from datetime import datetime

from live_mem.core.hivemind import (
    HivemindPeerChannel,
    HivemindStateStore,
    HivemindStateSnapshot,
    PeerKeyPair,
    PeerTransport,
)

from .clock import DeterministicClock

# FakeStorage est réutilisé tel quel depuis les tests #3 (cf. test_hivemind_peer.py).
from tests.test_hivemind_state import FakeStorage


class NodeRuntime:
    """
    Un nœud du cluster simulé : storage souverain + store + channel + clés.

    Le channel est (re)construit sur demande pour rester injectable par
    l'horloge logique partagée et reproductible après restart.
    """

    def __init__(
        self,
        *,
        node_id: str,
        space_id: str,
        keypair: PeerKeyPair,
        clock: DeterministicClock,
        transport: PeerTransport | None = None,
        storage: FakeStorage | None = None,
        replay_window_seconds: int = 300,
    ) -> None:
        self.node_id = node_id
        self.space_id = space_id
        self.keypair = keypair
        self._clock = clock
        self._transport = transport
        self._replay_window_seconds = replay_window_seconds
        # Le storage SURVIT au crash/restart : c'est le S3 persistant du nœud.
        self.storage: FakeStorage = storage or FakeStorage()
        self.alive = True
        self.store: HivemindStateStore = HivemindStateStore(
            storage=self.storage, space_id=space_id  # type: ignore[arg-type]
        )
        self.channel: HivemindPeerChannel = self._build_channel()

    def _build_channel(self) -> HivemindPeerChannel:
        return HivemindPeerChannel(
            state=self.store,
            local_node_id=self.node_id,
            private_key=self.keypair.private_key,
            transport=self._transport,
            clock=self._clock.now,
            replay_window_seconds=self._replay_window_seconds,
        )

    # ─────────────────────────────────────────────────────────────────
    # Crash / restart
    # ─────────────────────────────────────────────────────────────────

    def crash(self) -> None:
        """
        Simule un crash : la mémoire de process (channel + intent non flushé)
        disparaît. Le storage S3 reste intact (writes atomiques par objet).

        On marque le nœud mort et on détache le channel pour qu'aucun message
        ne soit servi tant que le nœud n'a pas redémarré — toute tentative est
        un bug du scénario.
        """
        self.alive = False
        self.channel = None  # type: ignore[assignment]

    async def restart(self) -> HivemindStateSnapshot:
        """
        Redémarre le nœud sur le MÊME storage : nouveau store + channel, puis
        ``load_snapshot`` pour reconstruire la vue protocole (cold-start réel).

        Retourne le snapshot rechargé pour inspection dans les tests.
        """
        self.store = HivemindStateStore(
            storage=self.storage, space_id=self.space_id  # type: ignore[arg-type]
        )
        self.channel = self._build_channel()
        self.alive = True
        return await self.store.load_snapshot()

    # ─────────────────────────────────────────────────────────────────
    # Helpers d'accès
    # ─────────────────────────────────────────────────────────────────

    def now(self) -> datetime:
        return self._clock.now()

    async def snapshot(self) -> HivemindStateSnapshot:
        return await self.store.load_snapshot()
