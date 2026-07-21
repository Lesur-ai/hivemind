# -*- coding: utf-8 -*-
"""
Enrôlement repo-driven et droits scopés des peers (P5-9, issue #103, ADR-0016).

Ce module pose la SOURCE DE VÉRITÉ déclarative de qui peut rejoindre un space
(le *manifest d'enrôlement*, fichier versionné) et la réconcilie DANS la
membership view via la machinerie membership existante (issue #5) — sans
nouveau chemin d'écriture privilégié, sans nouveau type d'event.

Invariants portés ici (ADR-0016) :

- **Default-deny / fail-closed (ADR-0008)** : un manifest manquant, non signé
  par un enrôleur autorisé, mal formé ou schéma-invalide N'APPLIQUE RIEN et ne
  retombe JAMAIS sur de l'« enrôlement ouvert ». La space reste sur sa dernière
  membership validement appliquée et l'erreur est BLOQUANTE (``EnrollmentError``).
- **Narrowing additif** : les scopes ne RESTREIGNENT que ce que le protocole
  gate déjà ; ``peer_scope_guard`` est une PRÉCONDITION amont, jamais un
  substitut au token / ``assert_commit_allowed()`` (ADR-0011).
- **Plancher ``read`` pour tout ACTIVE** : un peer enrôlé (qui devient/reste
  ACTIVE) DOIT détenir au moins ``read``. Un ACTIVE est un ACKer attendu de
  l'all-ACK, mais ``TOKEN_ACK`` exige ``read`` (peer.py) : un ACTIVE sans
  ``read`` resterait dans le set d'ACKers tout en voyant ses ACK rejetés, et le
  full-mesh all-ACK ne convergerait jamais. Un manifest enrôlant/rescopant un
  ACTIVE sur un jeu sans ``read`` est REFUSÉ fail-closed (``EnrollmentError``,
  aucune mutation) — pas d'injection silencieuse de ``read``.
- **Réutilise la membership existante** : ``add_member`` / ``update_member_scopes``
  / ``evict_member`` (events ``PEER_JOINED`` / ``MEMBERSHIP_UPDATED`` /
  ``PEER_EVICTED``, bump d'epoch). Aucun NOUVEAU ``EventType`` : un join émet
  ``PEER_JOINED``, un re-scoping émet l'event de membership GÉNÉRIQUE EXISTANT
  ``MEMBERSHIP_UPDATED`` (via ``update_member_scopes``), une révocation émet
  ``PEER_EVICTED`` — tous déjà définis dans l'enum.
- **Mono-tenant par construction (ADR-0003)** : AUCUN objet tenant, AUCUNE RLS.
  ``space_id`` divergent + tout contexte de tenancy non reconnu → refus. L'édition
  OSS ne comprend aucun contexte de tenancy ; un futur ``PolicyProvider`` Portal
  délègue à ce module.
- **Seule la clé PUBLIQUE entre dans le manifest** : aucun modèle d'enrôlement ne
  porte de ``private_key`` ; rien n'est écrit sous ``_hivemind/`` à part la
  membership partagée.
- **Pas de Graph / long state, pas de timer** : l'enrôlement et la révocation ne
  dérivent que du manifest et de l'état ``_hivemind/`` ; la révocation est un
  delta de manifest → ``evict_member`` (jamais une défédération par timer).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from pydantic import Field, field_validator

from .layout import PROTOCOL_VERSION
from .lifecycle import BootstrapError, MembershipService
from .models import (
    FULL_PEER_SCOPES,
    Member,
    MemberStatus,
    MembershipView,
    PeerScope,
    _HivemindBase,
)
from .peer import (
    PeerChannelError,
    PeerErrorCode,
    _b64decode,
    _canonical_json_bytes,
    _load_public_key,
)
from .state import HivemindStateStore


# =============================================================================
# Exceptions et énumérations
# =============================================================================


class EnrollmentError(RuntimeError):
    """
    Réconciliation échouée fail-closed : AUCUN changement de membership appliqué,
    signal BLOQUANT levé, JAMAIS un repli sur de l'enrôlement ouvert
    (ADR-0008 / ADR-0016).
    """


class EnrollmentState(str, Enum):
    """État déclaré d'un peer dans le manifest."""

    ENROLLED = "enrolled"
    REVOKED = "revoked"


# =============================================================================
# Modèles du manifest d'enrôlement (fichier versionné, signé)
# =============================================================================


class EnrollmentPeer(_HivemindBase):
    """Un peer déclaré dans le manifest : identité publique + état + scopes."""

    node_id: str
    public_key: str  # Ed25519, préfixe "ed25519:"
    endpoint: str = ""
    state: EnrollmentState = EnrollmentState.ENROLLED
    scopes: list[str] = Field(default_factory=lambda: sorted(FULL_PEER_SCOPES))

    @field_validator("node_id")
    @classmethod
    def _validate_node_id(cls, v: str) -> str:
        if not v:
            raise ValueError("node_id ne peut pas être vide")
        if "/" in v:
            raise ValueError("node_id ne doit pas contenir '/'")
        return v

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, v: list[Any]) -> list[str]:
        allowed = {s.value for s in PeerScope}
        out: list[str] = []
        for s in v:
            sv = s.value if isinstance(s, PeerScope) else s
            if sv not in allowed:
                raise ValueError(
                    "scope hors vocabulaire fermé {read,propose,commit}: "
                    f"{sv!r}"
                )
            if sv not in out:
                out.append(sv)
        return sorted(out)

    @field_validator("public_key")
    @classmethod
    def _validate_public_key(cls, v: str) -> str:
        # Clé mal formée → schéma-invalide → la porte fail-closed du reconcile
        # la convertit en EnrollmentError (jamais un peer empoisonné en
        # membership, cf. INVALID_KEY côté peer channel).
        try:
            _load_public_key(v)
        except PeerChannelError as exc:
            raise ValueError(f"public_key non-Ed25519: {exc}") from exc
        return v


class EnrollmentManifest(_HivemindBase):
    """
    Manifest d'enrôlement versionné et signé (la déclaration d'intention).

    La signature couvre le JSON canonique des HEADERS critiques
    (``protocol_version``, ``space_id``, ``peers``) et exclut le champ
    ``signature`` lui-même. Seule la clé publique d'un enrôleur autorisé peut
    produire une signature valide ; toute altération des peers casse la vérif.
    """

    protocol_version: int = PROTOCOL_VERSION
    space_id: str
    peers: list[EnrollmentPeer] = Field(default_factory=list)
    enroller_public_key: str = ""  # Ed25519 de l'enrôleur autorisé
    signature: str = ""  # Ed25519 sur canonical(headers + peers)

    @field_validator("peers")
    @classmethod
    def _validate_peers(cls, v: list[EnrollmentPeer]) -> list[EnrollmentPeer]:
        ids: set[str] = set()
        keys: set[str] = set()
        for p in v:
            if p.node_id in ids:
                raise ValueError(
                    f"node_id dupliqué dans le manifest: {p.node_id!r}"
                )
            if p.public_key in keys:
                raise ValueError(
                    "public_key dupliquée dans le manifest — identité ambiguë"
                )
            ids.add(p.node_id)
            keys.add(p.public_key)
        return v


# =============================================================================
# Seam de vérification de scope (ADR-0003 PolicyProvider, repli mono-tenant)
# =============================================================================


def peer_scope_guard(
    member: Member,
    required_scope: PeerScope,
    *,
    tenancy_context: Any = None,
) -> None:
    """
    Dénie (fail-closed) si ``member`` n'a pas ``required_scope``.

    PRÉCONDITION amont UNIQUEMENT — JAMAIS un substitut au token gate ni à
    ``assert_commit_allowed()`` (ADR-0011). Les scopes narrow, ne widen jamais :
    cette fonction ne fait qu'interdire plus tôt, jamais autoriser une écriture.
    Elle n'importe ni n'appelle aucune couche lease/token.

    Garde mono-tenant (posture ADR-0003) : tout ``tenancy_context`` non-vide /
    non reconnu → refus. L'édition OSS ne reconnaît AUCUN contexte de tenancy ;
    un futur ``PolicyProvider`` Portal délègue à ce helper.
    """
    if tenancy_context:
        raise PeerChannelError(
            PeerErrorCode.INSUFFICIENT_SCOPE,
            "contexte de tenancy non reconnu (OSS mono-tenant) — refus",
            {
                "signer_node_id": member.node_id,
                "tenancy_context": "unrecognized",
            },
        )
    if not member.has_scope(required_scope):
        raise PeerChannelError(
            PeerErrorCode.INSUFFICIENT_SCOPE,
            f"peer {member.node_id!r} sans scope {required_scope.value!r}",
            {
                "signer_node_id": member.node_id,
                "required_scope": required_scope.value,
                "granted_scopes": sorted(member.effective_scopes()),
            },
        )


# =============================================================================
# Réconciliation manifest → membership view
# =============================================================================


@dataclass(frozen=True)
class ReconcileResult:
    """Résultat d'une réconciliation (read-only, immuable)."""

    applied: bool
    epoch_before: int
    epoch_after: int
    joined: tuple[str, ...]
    revoked: tuple[str, ...]
    rescoped: tuple[str, ...]
    unchanged: tuple[str, ...]


@dataclass(frozen=True)
class _ReconcilePlan:
    """Plan d'application calculé en preflight (interne, read-only).

    Sépare la DÉCISION (calculée et validée sans mutation) de l'EXÉCUTION : le
    preflight ayant déjà rejeté tout conflit sémantique, l'application du plan
    ne peut plus laisser d'état partiel ni avaler un ``BootstrapError`` légitime.
    """

    add: list[tuple[str, Optional[list[str]]]]
    rescope: list[tuple[str, Optional[list[str]]]]
    revoke: list[str]
    unchanged: list[str]
    endpoints: dict[str, str]
    public_keys: dict[str, str]


def _narrow(scopes: list[str]) -> Optional[list[str]]:
    """
    Normalise un jeu de scopes accordé en la forme octet-pour-octet du membre.

    Un grant PLEIN (les 3 scopes) ⇒ ``None`` ⇒ ``Member.scopes is None`` ⇒
    sérialise comme un membre legacy (aucune clé ``scopes``). C'est OBLIGATOIRE :
    sans ça un peer full-rights enrôlé écrirait ``"scopes": [...]`` et divergerait
    octet-pour-octet d'un node non upgradé. Sinon, la liste triée déterministe.
    """
    return None if frozenset(scopes) == FULL_PEER_SCOPES else sorted(scopes)


class EnrollmentService:
    """
    Réconcilie un manifest d'enrôlement signé DANS la membership view.

    Toutes les portes de validation passent AVANT toute mutation (ADR-0008
    fail-closed) ; chaque delta est appliqué via ``MembershipService`` (jamais
    via ``set_membership`` direct) pour réutiliser le bump d'epoch + les events
    ``PEER_JOINED`` / ``PEER_EVICTED`` existants.
    """

    def __init__(
        self,
        store: HivemindStateStore,
        membership: MembershipService,
        *,
        trusted_enroller_keys: frozenset[str],
    ) -> None:
        self._store = store
        self._membership = membership
        self._trusted_enroller_keys = trusted_enroller_keys

    @staticmethod
    def _signature_payload(manifest: EnrollmentManifest) -> bytes:
        """JSON canonique signé : headers + peers, SANS le champ signature."""
        return _canonical_json_bytes(
            {
                "protocol_version": manifest.protocol_version,
                "space_id": manifest.space_id,
                "peers": [p.model_dump(mode="json") for p in manifest.peers],
            }
        )

    async def reconcile(self, raw_manifest: str | bytes) -> ReconcileResult:
        """
        Réconcilie ``raw_manifest`` dans la membership view (fail-closed).

        Ordre des portes — TOUTES avant la moindre mutation :
          1. manifest absent/vide → ``EnrollmentError`` ;
          2. JSON invalide ou schéma invalide (extra=forbid, clé mal formée,
             scope inconnu, node_id/clé dupliqué) → ``EnrollmentError`` ;
          3. ``protocol_version`` incompatible → ``EnrollmentError`` ;
          4. enrôleur non autorisé / signature invalide → ``EnrollmentError`` ;
          5. ``space_id`` divergent (garde mono-tenant) → ``EnrollmentError``.

        Ensuite seulement : calcul du diff vs. la ``MembershipView`` courante et
        application de chaque delta via ``MembershipService``.
        """
        # --- Porte 1 : présence ---------------------------------------------
        if raw_manifest is None or (
            isinstance(raw_manifest, (str, bytes)) and len(raw_manifest) == 0
        ):
            raise EnrollmentError(
                "manifest d'enrôlement absent/vide — fail-closed, aucun "
                "enrôlement ouvert"
            )

        # --- Porte 2 : JSON + schéma ----------------------------------------
        try:
            data = json.loads(raw_manifest)
        except (json.JSONDecodeError, ValueError) as exc:
            raise EnrollmentError(
                f"manifest d'enrôlement JSON invalide — fail-closed ({exc})"
            ) from exc
        try:
            manifest = EnrollmentManifest.model_validate(data)
        except Exception as exc:  # pydantic ValidationError + autres
            raise EnrollmentError(
                f"manifest d'enrôlement schéma invalide — fail-closed ({exc})"
            ) from exc

        # --- Porte 3 : protocol_version -------------------------------------
        if manifest.protocol_version != PROTOCOL_VERSION:
            raise EnrollmentError(
                "protocol_version incompatible dans le manifest "
                f"({manifest.protocol_version} != {PROTOCOL_VERSION})"
            )

        # --- Porte 4 : signature de l'enrôleur autorisé ---------------------
        if manifest.enroller_public_key not in self._trusted_enroller_keys:
            raise EnrollmentError(
                "enrôleur non autorisé — fail-closed (clé absente de la racine "
                "de confiance)"
            )
        try:
            signature = _b64decode(manifest.signature, 64)
            _load_public_key(manifest.enroller_public_key).verify(
                signature, self._signature_payload(manifest)
            )
        except EnrollmentError:
            raise
        except Exception as exc:
            raise EnrollmentError(
                f"signature manifest invalide — fail-closed ({exc})"
            ) from exc

        # --- Porte 5 : garde mono-tenant (space_id) -------------------------
        if manifest.space_id != self._store.space_id:
            raise EnrollmentError(
                "space_id du manifest divergent de la space cible "
                f"({manifest.space_id!r} != {self._store.space_id!r}) — "
                "garde mono-tenant"
            )

        # --- Lecture + plan + application ATOMIQUE sous UN verrou membership --
        # Atomicité (ADR-0008) : on tient le MÊME verrou par-space que
        # ``MembershipService`` pendant TOUTE la séquence lecture→plan→écriture,
        # et l'application est UNE SEULE ``set_membership`` (un seul bump d'epoch,
        # une seule écriture du fichier members.json) calculée par
        # ``apply_membership_plan``. Conséquence :
        #   * Concurrence : aucune mutation membership concurrente ne peut
        #     s'intercaler entre le read de preflight et l'écriture — le plan est
        #     calculé À PARTIR de la vue verrouillée qu'on va muter.
        #   * Tout-ou-rien : un seul write ⇒ JAMAIS d'état partiel. Si la
        #     construction de la vue cible échoue (invariant violé), AUCUNE
        #     écriture n'a eu lieu ; la membership reste exactement la précédente.
        # ``apply_membership_plan`` traduit toute ``BootstrapError`` d'invariant en
        # ``EnrollmentError`` : un plan périmé/conflictuel échoue fail-closed et
        # n'applique RIEN (jamais avalé, jamais partiel).
        operator = f"enrollment:{manifest.enroller_public_key[:16]}"
        async with self._membership._space_lock():  # noqa: SLF001 (atomicité)
            view = await self._store.get_membership()
            if view is None:
                raise EnrollmentError(
                    "membership absente — la réconciliation exige un space "
                    "Hivemind déjà initialisé (fail-closed)"
                )
            epoch_before = view.epoch

            # PREFLIGHT : plan complet + rejet de tout conflit sémantique AVANT
            # la moindre écriture. Calculé sur la vue VERROUILLÉE courante —
            # aucun écart « vue de preflight » vs « vue d'application ».
            plan = self._plan(view, manifest)

            has_deltas = bool(plan.add or plan.rescope or plan.revoke)
            if not has_deltas:
                # Plan vide (tout ``unchanged``) : aucune écriture, AUCUN bump
                # d'epoch — l'idempotence exige une réconciliation no-op stable.
                epoch_after = epoch_before
            else:
                try:
                    after = await self._membership.apply_membership_plan(
                        add=[
                            Member(
                                node_id=node_id,
                                endpoint=plan.endpoints[node_id],
                                public_key=plan.public_keys[node_id],
                                scopes=narrowed,
                            )
                            for node_id, narrowed in plan.add
                        ],
                        rescope=plan.rescope,
                        revoke=plan.revoke,
                        operator=operator,
                        reason="manifest revoked",
                        base_view=view,
                    )
                except BootstrapError as exc:
                    # Invariant membership violé à l'application (plan devenu
                    # incohérent vs. la vue verrouillée, p.ex. un appelant qui
                    # aurait pré-snapshotté une vue périmée) → fail-closed, AUCUNE
                    # écriture n'a eu lieu (la vue cible est construite EN MÉMOIRE
                    # avant le write unique).
                    raise EnrollmentError(
                        f"application du plan refusée fail-closed ({exc}) — "
                        "aucune mutation"
                    ) from exc
                epoch_after = after.epoch if after is not None else epoch_before

        joined = tuple(node_id for node_id, _ in plan.add)
        rescoped = tuple(node_id for node_id, _ in plan.rescope)
        applied = bool(joined or plan.revoke or rescoped)

        return ReconcileResult(
            applied=applied,
            epoch_before=epoch_before,
            epoch_after=epoch_after,
            joined=joined,
            revoked=tuple(plan.revoke),
            rescoped=rescoped,
            unchanged=tuple(plan.unchanged),
        )

    def _plan(
        self, view: MembershipView, manifest: EnrollmentManifest
    ) -> "_ReconcilePlan":
        """Calcule le plan d'application ET rejette les conflits sémantiques.

        Lecture seule : aucune mutation. Lève ``EnrollmentError`` (fail-closed)
        si le manifest demande une transition impossible/dangereuse — la même
        famille de fautes que ``MembershipService`` lève en ``BootstrapError``
        (clé publique active dupliquée, retrait du dernier membre actif), mais
        détectée AVANT toute écriture pour ne JAMAIS laisser d'état partiel.
        """
        active_by_id: dict[str, Member] = {
            m.node_id: m
            for m in view.members
            if m.status == MemberStatus.ACTIVE.value
        }
        # Set des node_id qui resteront/seront ACTIVE après application, et
        # index clé publique -> node_id pour détecter les collisions d'identité.
        revoked_ids = {
            p.node_id
            for p in manifest.peers
            if p.state == EnrollmentState.REVOKED
        }
        surviving_keys: dict[str, str] = {
            m.public_key: m.node_id
            for nid, m in active_by_id.items()
            if nid not in revoked_ids and m.public_key
        }

        add: list[tuple[str, Optional[list[str]]]] = []
        rescope: list[tuple[str, Optional[list[str]]]] = []
        revoke: list[str] = []
        unchanged: list[str] = []
        endpoints: dict[str, str] = {}
        public_keys: dict[str, str] = {}

        for peer in manifest.peers:
            narrowed = _narrow(peer.scopes)
            current = active_by_id.get(peer.node_id)

            if peer.state == EnrollmentState.REVOKED:
                if current is not None:
                    # Le manifest est la source de vérité du tuple (node_id,
                    # public_key, state). Révoquer un membre ACTIVE depuis une
                    # public_key qui NE correspond PAS à sa clé persistée est un
                    # tuple d'identité incohérent : l'appliquer serait une
                    # éviction destructive décidée depuis une identité non
                    # vérifiée — exactement la rotation de clé implicite que le
                    # chemin ENROLLED refuse (cf. infra). On REFUSE fail-closed
                    # AVANT toute écriture (ADR-0008) ; une révocation légitime
                    # DOIT citer la clé courante du membre.
                    if current.public_key != peer.public_key:
                        raise EnrollmentError(
                            f"révocation du membre ACTIVE {peer.node_id!r} avec "
                            "une public_key ne correspondant pas à la clé "
                            "persistée — tuple d'identité incohérent, manifest "
                            "refusé fail-closed (aucune mutation)"
                        )
                    revoke.append(peer.node_id)
                else:
                    # Déjà absent / non-ACTIVE : re-révocation = no-op.
                    unchanged.append(peer.node_id)
                continue

            # ENROLLED → le peer sera/restera ACTIVE, donc un ACKer attendu de
            # l'all-ACK. PLANCHER ``read`` (ADR-0016) : tout membre ACTIVE DOIT
            # détenir ``read``. Un ACTIVE sans ``read`` voit ses ``TOKEN_ACK``
            # rejetés en ``INSUFFICIENT_SCOPE`` (peer.py: TOKEN_ACK -> read)
            # alors qu'il reste dans le set d'ACKers attendus
            # (``expected_ack_node_ids``) — le full-mesh all-ACK ne convergerait
            # JAMAIS (blocage permanent). On REFUSE fail-closed AVANT toute
            # écriture (jamais d'injection silencieuse de ``read`` : la
            # misconfiguration DOIT être corrigée dans le manifest).
            if PeerScope.READ.value not in peer.scopes:
                raise EnrollmentError(
                    f"membre ACTIVE {peer.node_id!r} enrôlé/rescopé sans le "
                    f"scope plancher 'read' (scopes={sorted(peer.scopes)!r}) — "
                    "un ACTIVE sans 'read' ne peut servir/ACK et bloquerait "
                    "l'all-ACK, manifest refusé fail-closed (aucune mutation)"
                )

            if current is None:
                # Collision d'identité : clé publique déjà détenue par un AUTRE
                # membre qui survivra (genuine BootstrapError côté add_member).
                # On la REFUSE fail-closed AVANT toute écriture (jamais avalée).
                owner = surviving_keys.get(peer.public_key)
                if owner is not None and owner != peer.node_id:
                    raise EnrollmentError(
                        "public_key déjà active pour un autre node "
                        f"({owner!r}) — identité ambiguë, manifest refusé "
                        "(aucune mutation)"
                    )
                surviving_keys[peer.public_key] = peer.node_id
                endpoints[peer.node_id] = peer.endpoint
                public_keys[peer.node_id] = peer.public_key
                add.append((peer.node_id, narrowed))
            elif current.public_key != peer.public_key:
                # Rotation de clé SILENCIEUSE d'un membre ACTIVE : le manifest est
                # la source de vérité de (node_id, public_key, scopes), mais le
                # rescope ne touche QUE les scopes ; appliquer cette entrée
                # laisserait runtime `_verify` faire confiance à l'ANCIENNE clé
                # persistée (peer.py: member.public_key), donc une clé périmée /
                # compromise resterait autorisée. La rotation explicite est une
                # FEATURE future séparée ; en V1 on REFUSE fail-closed AVANT toute
                # écriture — jamais de swap implicite, aucune mutation (ADR-0008).
                raise EnrollmentError(
                    f"public_key change pour le membre ACTIVE {peer.node_id!r} — "
                    "rotation de clé implicite non supportée en V1, manifest "
                    "refusé fail-closed (aucune mutation)"
                )
            elif current.scopes != narrowed:
                rescope.append((peer.node_id, narrowed))
            else:
                unchanged.append(peer.node_id)

        # Garde « dernier membre actif » : le plan ne doit pas vider la
        # membership de tout participant ACTIVE (même faute que la garde de
        # ``evict_member``), sinon le space paraîtrait non-Hivemind.
        remaining_active = (set(active_by_id) - set(revoke)) | {
            node_id for node_id, _ in add
        }
        if not remaining_active:
            raise EnrollmentError(
                "réconciliation refusée : le manifest retirerait le dernier "
                "membre ACTIVE — un space sans actif paraîtrait non-Hivemind "
                "(aucune mutation)"
            )

        # Note (limite S3, transaction multi-clés) : le cas « remplacer le SEUL
        # membre actif par un nouveau node RÉUTILISANT sa clé publique » serait
        # irrésoluble par ordre d'application (revoke-first vide la membership,
        # add-first collisionne sur la clé). Il est cependant DÉJÀ impossible à
        # exprimer : ``EnrollmentManifest._validate_peers`` interdit deux peers de
        # même public_key. L'application atomique single-write rend par ailleurs
        # le remplacement à clé DIFFÉRENTE (revoke nodeA + add nodeB) sûr et sans
        # état « zéro actif » intermédiaire.

        return _ReconcilePlan(
            add=add,
            rescope=rescope,
            revoke=revoke,
            unchanged=unchanged,
            endpoints=endpoints,
            public_keys=public_keys,
        )
