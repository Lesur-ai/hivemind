# -*- coding: utf-8 -*-
"""
Politique d'ACK — seam d'extensibilité (issue #11).

V1 n'a QU'UNE implémentation : ``AllAckPolicy``. Le progrès n'est autorisé que
si CHAQUE membre ACTIF a personnellement ACKé (all-ACK strict par IDENTITÉ,
jamais quorum, jamais un simple comptage).

Pourquoi par identité et non par comptage : un comptage (``received >=
expected``) laisse un ACK périmé, évincé ou inconnu se substituer à un membre
actif manquant et autoriser quand même le grant. La satisfaction est donc
validée sur des ENSEMBLES : l'ensemble des ``ack_by_node_id`` reçus doit être
un sur-ensemble de l'ensemble des ``node_id`` actifs (tout actif a ACKé). Un
ACK d'un nœud non-actif/évincé ne peut combler le trou d'un actif manquant.

La politique est exprimée comme une stratégie injectée pour que les travaux
quorum/hub futurs (#?, hors V1) puissent fournir une autre implémentation SANS
toucher aux tests d'invariants V1. Un test garde-fou
(``test_v1_all_ack_policy_is_default_and_blocking``) épingle ``AllAckPolicy``
comme défaut et échoue si du code accepte un progrès alors qu'un membre actif
n'a pas ACKé — c'est le filet anti-relâchement du non-goal « pas de quorum en
V1 ».
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from live_mem.core.hivemind import MembershipView, MemberStatus


class AckPolicy(Protocol):
    """Décide quels ACK sont attendus et si le progrès est autorisé."""

    def expected_acks(self, membership: MembershipView) -> int: ...

    def expected_ack_set(self, membership: MembershipView) -> set[str]: ...

    def is_satisfied(
        self, *, received: Iterable[str], membership: MembershipView
    ) -> bool: ...

    @property
    def name(self) -> str: ...


class AllAckPolicy:
    """
    All-ACK conservateur (HIVEMIND.md §6.1). C'est la SEULE politique V1.

    L'ensemble attendu = les ``node_id`` des membres ACTIFS (les EVICTED/LEAVING
    ne comptent pas). Le progrès exige que l'ensemble des ACKers REÇUS soit un
    sur-ensemble de cet ensemble attendu : chaque actif a ACKé EN PERSONNE. Un
    ACK supplémentaire d'un nœud non-actif ne satisfait jamais à la place d'un
    actif manquant — la validation est par identité, pas par cardinalité.
    """

    name = "all_ack"

    @staticmethod
    def expected_ack_set(membership: MembershipView) -> set[str]:
        return {
            m.node_id
            for m in membership.members
            if m.status == MemberStatus.ACTIVE.value
        }

    def expected_acks(self, membership: MembershipView) -> int:
        return len(self.expected_ack_set(membership))

    def is_satisfied(
        self, *, received: Iterable[str], membership: MembershipView
    ) -> bool:
        # all-ACK strict PAR IDENTITÉ : chaque membre actif doit figurer parmi
        # les ACKers reçus. Un ACK d'un nœud non-actif/évincé/inconnu ne peut
        # pas combler l'absence d'un actif (pas de substitution par comptage).
        received_set = set(received)
        expected = self.expected_ack_set(membership)
        return expected.issubset(received_set)
