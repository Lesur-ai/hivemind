# -*- coding: utf-8 -*-
"""
Harnais d'injection de fautes Hivemind V1 — TEST-ONLY (issue #11).

Ce package n'est PAS sous ``src/`` : il ne ship jamais et ne devient jamais une
seconde source de vérité pour la sémantique V1. Il est importable par les tests
(et par de futurs tests quorum/hub) mais ne contient aucun fichier ``test_*``
donc pytest ne le collecte pas comme suite de tests.

Composants :

- ``DeterministicClock`` : temps logique pur (aucune horloge murale, aucun
  ``sleep``) ; drop-in du seam ``clock=`` de ``HivemindPeerChannel``.
- ``FaultyTransport`` : implémente le ``PeerTransport`` Protocol publié ;
  livraison pull-based explicite avec drop / reorder / duplicate / delay /
  partition, déterministe (aléa seedé opt-in seulement).
- ``NodeRuntime`` : un nœud = ``FakeStorage`` souverain + ``HivemindStateStore``
  + ``HivemindPeerChannel`` + keypair ; ``crash()`` / ``restart()`` via
  ``load_snapshot`` sur le MÊME storage.
- ``ClusterHarness`` : N nœuds sur UN ``FaultyTransport``, membership/term
  semés identiques, primitives de pas + seam ``AckPolicy``.
- ``AllAckPolicy`` : seule politique V1 (expected = membres actifs).
- ``ProtocolModel`` : modèle de référence V1 pilotant les VRAIES primitives.
- ``assert_invariants`` et helpers : scannent TOUS les stores, lèvent une
  ``AssertionError`` nommant le nœud / term / bank_version fautif.

═══════════════════════════════════════════════════════════════════════════════
SUIVI POUR LES ISSUES AVAL (#6 / #7 / #8 / #12) — IMPÉRATIF
═══════════════════════════════════════════════════════════════════════════════
Quand les services réels atterrissent, ils doivent RE-POINTER le harnais sur le
vrai service derrière le MÊME seam (NodeRuntime / ProtocolModel) et garder les
assertions d'invariants INCHANGÉES :

- #6 QueueService        → remplace ``ProtocolModel.claim`` + l'allocation de
                           ``sequence`` (tri déterministe des claims concurrents).
- #7 TokenLeaseService   → remplace ``ProtocolModel.grant`` / ``release`` et
                           l'évaluation d'expiration de lease
                           (``is_lease_expired``).
- #8 CommitService       → remplace ``ProtocolModel.apply_commit`` ET porte le
                           commit-apply fencing guard (aujourd'hui dans le
                           modèle car ``append_commit`` ne valide pas le term).
                           Doit aussi introduire les EventType manquants
                           (COMMIT_PROPOSED / COMMIT_REJECTED, etc.).
- #12 ReplicationService → remplace la réplication live-note / tombstone / GC
                           croisée et l'observation de watermark.

EventType manquants à AJOUTER dans src/ par les issues concernées (PAS ici) :
QUEUE_REQUEST (ou conserver le mapping → TOKEN_CLAIM), RESYNC_REQUESTED /
RESYNC_COMPLETED, PEER_EVICTED / PEER_JOINED, LEASE_EXPIRED / TOKEN_FENCED.
Ces concepts vivent en couche test ici (cf. ``ProtocolModel`` / ``HiveStatus``).
"""

from __future__ import annotations

from .clock import DeterministicClock
from .cluster import ClusterHarness
from .invariants import (
    assert_at_most_one_valid_holder,
    assert_bank_version_monotone,
    assert_commits_consistent,
    assert_invariants,
    assert_membership_epoch_monotone,
    assert_no_stale_term_commit,
    assert_no_tombstone_resurrection,
    assert_term_monotone,
)
from .model import CommitFencedError, HiveStatus, ProtocolModel
from .node import NodeRuntime
from .policy import AckPolicy, AllAckPolicy
from .transport import FaultyTransport

__all__ = [
    "DeterministicClock",
    "FaultyTransport",
    "NodeRuntime",
    "ClusterHarness",
    "AckPolicy",
    "AllAckPolicy",
    "ProtocolModel",
    "HiveStatus",
    "CommitFencedError",
    "assert_invariants",
    "assert_at_most_one_valid_holder",
    "assert_term_monotone",
    "assert_bank_version_monotone",
    "assert_membership_epoch_monotone",
    "assert_no_stale_term_commit",
    "assert_commits_consistent",
    "assert_no_tombstone_resurrection",
]
