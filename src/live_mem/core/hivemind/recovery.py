# -*- coding: utf-8 -*-
"""
Déclencheurs de récupération manuelle Hivemind (issue #12 / P5-4 ; ADR-0014).

Délégation MINCE aux services de cycle de vie P5-1 (``MembershipService`` /
``ResyncService``). Ce module n'invente AUCUNE primitive de mutation et
n'enregistre AUCUN outil MCP : il ne fait qu'ajouter une **traduction de code
d'erreur** structuré (taxonomie ``PeerErrorCode``) au-dessus des services
existants, et une pré-vérification de permission AVANT toute délégation.

Les trois codes opérateur (surfacés via ``PeerChannelError.to_dict()``) :

- ``PERMISSION_DENIED`` — l'appelant n'a pas le scope opérateur (operator vide
  ou ``confirm`` faux). Levé AVANT tout appel de service ⇒ zéro écriture.
- ``PROTOCOL_BLOCKED`` — la santé du hive interdit la mutation (refus
  fail-closed traduit du ``BootstrapError`` de ``_current_view`` ; couvre aussi
  « node absent » / « dernier membre actif »).
- ``READ_ONLY_ALLOWED`` — sentinelle du chemin de lecture (cf. ``status.py``).

Isolation : aucun import mémoire longue / graph / consolidateur ; aucun timer.
"""

from __future__ import annotations

from .lifecycle import (
    BootstrapError,
    MembershipService,
    ResyncService,
    resolve_hive_context,
)
from .models import HiveNodeStatus, MemberStatus, MembershipView, NodeHealth
from .peer import PeerChannelError, PeerErrorCode
from .state import HivemindStateStore


class RecoveryTriggers:
    """
    Délégation mince aux services P5-1 ``MembershipService`` / ``ResyncService``.

    AUCUNE primitive de mutation propre ; AUCUN outil MCP. N'ajoute qu'une
    traduction de code d'erreur et une pré-vérification de permission.
    """

    def __init__(self, store: HivemindStateStore) -> None:
        self._store = store
        self._membership = MembershipService(store)
        self._resync = ResyncService(store)

    async def _resolve_hive_or_block(self) -> None:
        """Résout le contexte Hivemind du space et REFUSE fail-closed
        (``PROTOCOL_BLOCKED``) si le space n'est PAS un hive résolu et sûr.

        Garde portée AVANT toute délégation à ``ResyncService`` : sans elle, un
        appel opérateur confirmé sur un space NON-Hivemind (ou structurellement
        incomplet / corrompu) ferait écrire à ``observe_remote`` un
        ``node_status.json`` + un event ``RESYNC_REQUIRED`` (``observe_remote``
        traite une membership absente comme epoch ``0`` et un pointeur absent
        comme ``-1``). Cela fabriquerait de l'état Hivemind sur un space legacy,
        violant l'invariant non-Hivemind octet-pour-octet.

        Refus fail-closed sur :
        - ``is_hive == False`` (space legacy/local) -> JAMAIS d'état Hivemind ;
        - ``node_status == UNSAFE`` (structure incomplète / import partiel /
          corruption résiduelle) -> le space n'est pas dans un état sûr pour
          muter la santé ;
        - contexte STRUCTURELLEMENT INCOMPLET (identité ``node.json`` absente OU
          aucun membre ACTIVE), même quand ``node_status`` est un marqueur
          ``RESYNC_REQUIRED`` (ou ``HEALTHY``) solitaire. ``resolve_hive_context``
          respecte un marqueur ``RESYNC_REQUIRED`` explicite tel quel (sans le
          dégrader en ``UNSAFE``) même sans ``node.json`` ni membre ACTIVE :
          ``is_hive`` y est ``True`` et ``node_status`` n'est PAS ``UNSAFE``, donc
          les deux gardes ci-dessus le laisseraient passer. Sans cette garde de
          complétude, ``observe_remote`` muterait un contexte incomplet (epoch
          absent traité comme 0, pointeur absent comme -1) et
          ``mark_resync_complete`` passerait son rattrapage sur des valeurs
          locales par défaut puis écrirait ``HEALTHY`` + un event
          ``RESYNC_COMPLETED`` sur un hive inexistant.

        AUTORISE uniquement un contexte COMPLET (identité présente ET >= 1 membre
        ACTIVE) en ``HEALTHY`` (cas primaire : un node sain apprend qu'il est en
        retard) ou ``RESYNC_REQUIRED`` (re-observation idempotente d'un node déjà
        marqué). Une ``CorruptedStateError`` levée par la résolution PROPAGE
        (fail-closed), elle n'est jamais traduite en code de raison.
        """
        ctx = await resolve_hive_context(self._store._storage, self._store.space_id)
        if not ctx.is_hive:
            raise PeerChannelError(
                PeerErrorCode.PROTOCOL_BLOCKED,
                "resync refused: non-Hivemind (legacy/local) space — a "
                "resync trigger never creates Hivemind state on an unshared "
                "space (non-Hivemind invariant remains byte-for-byte)",
                {"is_hive": False, "node_status": ctx.node_status.value},
            )
        if ctx.node_status == HiveNodeStatus.UNSAFE:
            raise PeerChannelError(
                PeerErrorCode.PROTOCOL_BLOCKED,
                "resync refused: unsafe Hivemind context (UNSAFE — incomplete "
                "structure / partial import) — repair is required before any "
                "health mutation",
                {"is_hive": True, "node_status": ctx.node_status.value},
            )
        has_active_member = ctx.membership is not None and any(
            m.status == MemberStatus.ACTIVE.value for m in ctx.membership.members
        )
        if ctx.node is None or not has_active_member:
            raise PeerChannelError(
                PeerErrorCode.PROTOCOL_BLOCKED,
                "resync refused: structurally INCOMPLETE Hivemind context "
                "(node.json identity is absent or there is no ACTIVE member) — a "
                "lone node_status marker is NOT a safe hive to mutate; repair is "
                "required before any health mutation",
                {
                    "is_hive": True,
                    "node_status": ctx.node_status.value,
                    "has_node_identity": ctx.node is not None,
                    "has_active_member": has_active_member,
                },
            )

    async def evict(
        self,
        node_id: str,
        *,
        operator: str,
        confirm: bool = False,
        reason: str = "",
    ) -> MembershipView:
        """
        Déclenche une éviction — délègue à ``MembershipService.evict_member``.

        Pré-vérifie ``operator`` + ``confirm`` et lève ``PERMISSION_DENIED``
        AVANT toute délégation (donc zéro écriture sur un refus de permission).
        Un refus de santé du hive (``BootstrapError`` de ``_current_view``,
        « node absent » ou « dernier membre actif ») est traduit en
        ``PROTOCOL_BLOCKED``.
        """
        if not operator or not confirm:
            raise PeerChannelError(
                PeerErrorCode.PERMISSION_DENIED,
                "eviction refused: operator + confirm=True required",
                {"node_id": node_id},
            )  # levé AVANT tout appel de service -> zéro écriture
        try:
            return await self._membership.evict_member(
                node_id, operator=operator, confirm=confirm, reason=reason
            )
        except BootstrapError as exc:
            raise PeerChannelError(
                PeerErrorCode.PROTOCOL_BLOCKED, str(exc), {"node_id": node_id}
            ) from exc

    async def request_resync(
        self,
        *,
        operator: str,
        confirm: bool = False,
        observed_epoch: int = -1,
        observed_bank_version: int = -1,
    ) -> NodeHealth:
        """
        Déclenche un resync — délègue à ``ResyncService.observe_remote``.

        MUTATION (écrit ``node_status.json`` + event ``RESYNC_REQUIRED`` quand
        l'observation est en avance), donc même contrat que ``evict`` : pré-
        vérifie ``operator`` + ``confirm`` et lève ``PERMISSION_DENIED`` AVANT
        toute délégation (donc zéro écriture sur un refus de permission), PUIS
        résout le contexte Hivemind et REFUSE ``PROTOCOL_BLOCKED`` fail-closed
        sur un space non-Hivemind / non sûr (``_resolve_hive_or_block``) AVANT
        de déléguer — ``observe_remote`` n'a aucune garde de contexte propre et
        fabriquerait sinon de l'état Hivemind sur un space legacy.
        """
        if not operator or not confirm:
            raise PeerChannelError(
                PeerErrorCode.PERMISSION_DENIED,
                "resync refused: operator + confirm=True required",
                {
                    "observed_epoch": observed_epoch,
                    "observed_bank_version": observed_bank_version,
                },
            )  # levé AVANT tout appel de service -> zéro écriture
        await self._resolve_hive_or_block()
        return await self._resync.observe_remote(
            observed_epoch=observed_epoch,
            observed_bank_version=observed_bank_version,
        )

    async def complete_resync(
        self,
        *,
        operator: str,
        confirm: bool = False,
    ) -> NodeHealth:
        """
        Marque le resync complété — délègue à
        ``ResyncService.mark_resync_complete``.

        MUTATION (repasse ``HEALTHY`` + event ``RESYNC_COMPLETED``), donc même
        contrat que ``evict`` : pré-vérifie ``operator`` + ``confirm`` et lève
        ``PERMISSION_DENIED`` AVANT toute délégation (zéro écriture sur refus),
        PUIS résout le contexte Hivemind et REFUSE ``PROTOCOL_BLOCKED``
        fail-closed sur un space non-Hivemind / non sûr
        (``_resolve_hive_or_block``) AVANT de déléguer. Un refus de protocole
        (``BootstrapError`` : état non ``RESYNC_REQUIRED`` ou rattrapage
        incomplet) est traduit en ``PROTOCOL_BLOCKED``.
        """
        if not operator or not confirm:
            raise PeerChannelError(
                PeerErrorCode.PERMISSION_DENIED,
                "resync completion refused: operator + confirm=True required",
                {},
            )  # levé AVANT tout appel de service -> zéro écriture
        await self._resolve_hive_or_block()
        try:
            return await self._resync.mark_resync_complete()
        except BootstrapError as exc:
            raise PeerChannelError(
                PeerErrorCode.PROTOCOL_BLOCKED, str(exc), {}
            ) from exc
