# -*- coding: utf-8 -*-
"""
Modèles Pydantic du protocole Hivemind V1.

Chaque objet ici représente une portion durable de l'état d'un space
participant à un cluster Hivemind. Les champs communs présents dans la
plupart des messages (``protocol_version``, ``membership_epoch``, ``term``,
``bank_version``, ``event_id``, ``request_id``) suivent le contrat défini
dans l'issue #3 et DESIGN/live-mem/HIVEMIND.md.

Invariants protocole portés par ces modèles :

- ``term`` est un compteur monotone croissant à l'échelle du space.
- ``membership_epoch`` est bumpé à chaque mutation de la membership view.
- ``bank_version`` est monotone croissant ; chaque commit doit référencer
  ``parent_bank_version = bank_version - 1`` (le 0 a un parent à -1).
- ``event_id`` est l'identifiant unique d'un message de protocole — il
  doit être stable sur replay (un client qui retente émet le même).
- ``request_id`` corrèle un side-effect protocole avec la requête initiale
  côté demandeur (utile pour la traçabilité).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
)

from .layout import PROTOCOL_VERSION

_logger = logging.getLogger("live_mem.hivemind")

# RA-3 fix (observabilité) : dédup des alertes « scopes absent → FULL ».
# On alerte UNE fois par node pour éviter le bruit (effective_scopes est appelé
# à chaque vérification d'event).
_warned_none_scopes: set[str] = set()


# =============================================================================
# Exceptions et énumérations
# =============================================================================


class CorruptedStateError(RuntimeError):
    """
    Levée par ``HivemindStateStore`` quand un fichier S3 est présent mais
    impossible à désérialiser (JSON cassé, schéma invalide, contraintes
    Pydantic violées).

    Le caller doit décider de la stratégie de récupération (backup,
    quarantine, refus de démarrer). On ne fait JAMAIS de "réparation
    silencieuse" : la corruption est un signal opérationnel.
    """


class QueueReplayConflictError(ValueError):
    """
    Levée quand un ``event_id`` déjà présent dans la queue est ré-soumis avec
    un payload logique DIVERGENT (``requester_node_id`` / ``term`` /
    ``membership_epoch`` / ``bank_version`` différents de l'entrée persistée).

    Même sémantique que ``REPLAY_CONFLICT`` côté ``peer.py`` (même ``event_id``
    + payload différent = ERREUR PROTOCOLE) : un identifiant d'événement
    identifie UN événement logique unique. Un rejeu fidèle (mêmes champs) est
    idempotent et retourne l'entrée existante ; un rejeu divergent est
    fail-closed (aucune seconde écriture durable), jamais coalescé en succès
    silencieux.

    Hérite de ``ValueError`` (erreur d'argument du caller, pas une corruption
    d'état persistant — d'où la distinction avec ``CorruptedStateError``).
    """


class MemberStatus(str, Enum):
    """
    Statut d'un peer dans la membership view **partagée** d'un space.

    C'est un état symétrique : tous les nœuds convergent vers la même valeur
    via le bump d'``epoch``. À NE PAS confondre avec ``HiveNodeStatus`` qui,
    lui, est purement **local** (santé de l'instance courante).
    """

    ACTIVE = "active"
    LEAVING = "leaving"
    EVICTED = "evicted"
    #: Candidat en cours d'enrôlement Project Mesh (P10-3, ADR-0024). Admis à
    #: l'epoch e+1 (Transition 1) puis promu ``ACTIVE`` à e+2 (Transition 2), un
    #: membre ``PENDING`` n'est JAMAIS compté ACTIVE : exclu de tout roster
    #: all-ACK / ordinary-write (``active_members`` / ``expected_ack_node_ids``),
    #: rejeté comme ``UNKNOWN_PEER`` par ``HivemindPeerChannel._verify`` (donc
    #: write-blocked), et ne détient aucune autorité (jamais ``last active``,
    #: jamais signataire d'enrôlement). La cible reste ``PENDING`` + node_status
    #: local unsafe (routing REFUSE) jusqu'à l'application prouvée du bump e+2.
    PENDING = "pending"


class PeerScope(str, Enum):
    """
    Vocabulaire **fermé** des droits d'un peer (ADR-0016).

    Narrowing additif uniquement : l'absence d'un scope DÉNIE ; un scope ne
    peut que RESTREINDRE ce que le protocole gate déjà, jamais accorder un
    bypass.

    - ``read`` : peut recevoir / servir l'état partagé.
    - ``propose`` : peut soumettre des entrées de queue / tentatives de claim.
    - ``commit`` : peut se voir accorder le token + émettre un ``BANK_COMMIT``.
      C'est une PRÉCONDITION amont, jamais un substitut à
      ``assert_commit_allowed()`` (ADR-0011).
    """

    READ = "read"
    PROPOSE = "propose"
    COMMIT = "commit"


#: Jeu de scopes COMPLET — le défaut de rétro-compatibilité. Un membre dont
#: ``scopes is None`` porte tous les scopes (comportement single-node préservé
#: octet-pour-octet jusqu'à ce qu'un manifest le restreigne).
FULL_PEER_SCOPES: frozenset[str] = frozenset(
    {PeerScope.READ.value, PeerScope.PROPOSE.value, PeerScope.COMMIT.value}
)


class HiveNodeStatus(str, Enum):
    """
    Santé **node-local** de l'instance courante pour un space Hivemind.

    Persisté dans ``_hivemind/node_status.json`` — un fichier critique
    séparé de la membership partagée (``members.json``). Une instance en
    retard ou en cours d'import ne doit JAMAIS muter la vue partagée ; elle
    ne touche que son propre ``node_status``.

    Valeurs (chaînes persistées, append-only) :

    - ``DISABLED`` : pas (encore) participant Hivemind, ou absence de fichier.
    - ``HEALTHY`` : état local cohérent, autorisé à participer.
    - ``RESYNC_REQUIRED`` : a observé un epoch futur ou une bank_version
      manquée ; doit resync depuis un peer/snapshot avant de progresser.
    - ``UNSAFE`` : état local potentiellement incohérent (import partiel,
      corruption détectée) ; aucune mutation tant qu'il n'est pas réparé.
    """

    DISABLED = "disabled"
    HEALTHY = "healthy"
    RESYNC_REQUIRED = "resync_required"
    UNSAFE = "unsafe"


class TokenState(str, Enum):
    FREE = "free"  # Personne ne tient le token.
    HELD = "held"  # Un nœud tient le token (lease en cours).
    RELEASING = "releasing"  # Le détenteur a annoncé le release, attend ACKs.


class QueueEntryStatus(str, Enum):
    PENDING = "pending"
    GRANTED = "granted"
    CANCELLED = "cancelled"


class EventType(str, Enum):
    """
    Types d'événements posés au journal append-only.

    La liste est volontairement restreinte au scope V1 ; les couches
    transport/membership/observabilité (issues #4-#12) en ajouteront.
    """

    MEMBERSHIP_UPDATED = "membership_updated"
    TERM_BUMPED = "term_bumped"
    TOKEN_CLAIM = "token_claim"
    TOKEN_GRANTED = "token_granted"
    TOKEN_RELEASED = "token_released"
    TOKEN_ACK = "token_ack"
    BANK_COMMITTED = "bank_committed"
    TOMBSTONE_RECORDED = "tombstone_recorded"
    WATERMARK_UPDATED = "watermark_updated"
    # Cycle de vie membership/bootstrap/resync (issue #5). Append-only :
    # ces valeurs sont des chaînes persistées dans le journal d'audit
    # (HIVEMIND.md §8), elles ne doivent jamais être renommées.
    PEER_JOINED = "peer_joined"
    PEER_EVICTED = "peer_evicted"
    RESYNC_REQUIRED = "resync_required"
    RESYNC_COMPLETED = "resync_completed"
    BOOTSTRAP_SNAPSHOT_EXPORTED = "bootstrap_snapshot_exported"
    BOOTSTRAP_SNAPSHOT_IMPORTED = "bootstrap_snapshot_imported"
    # P6-1 (issue #87, ADR-0014) : operator-confirmed unsafe recovery applied
    # via backup_restore over a Hivemind-marked space. Distinct from
    # RESYNC_REQUIRED so the audit trail preserves the explicit operator-
    # initiated forward-forcing event (epoch/term/token/bank_version/queue/
    # acks/watermarks/tombstones) from the resync invitation that follows.
    UNSAFE_RECOVERY_RESTORED = "unsafe_recovery_restored"


# =============================================================================
# Helpers
# =============================================================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _HivemindBase(BaseModel):
    """Base config commune : valider en assignation, refuser les champs inconnus."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
    )


# =============================================================================
# Identité et membership
# =============================================================================


class NodeIdentity(_HivemindBase):
    """
    Identité durable de l'instance ``live-memory`` locale pour un space donné.

    Chaque space porte sa propre identité — deux spaces du même hôte ont
    des ``node_id`` distincts pour éviter qu'un peering accidentel ne mélange
    les autorisations.
    """

    protocol_version: int = PROTOCOL_VERSION
    node_id: str  # UUID hex (32 chars sans tirets) idéalement
    display_name: str = ""
    public_key: str = ""  # Réservé pour issue #4 (peer auth)
    created_at: str = Field(default_factory=_now_iso)

    @field_validator("node_id")
    @classmethod
    def _validate_node_id(cls, v: str) -> str:
        if not v:
            raise ValueError("node_id cannot be empty")
        if "/" in v:
            raise ValueError("node_id must not contain '/'")
        return v


class Member(_HivemindBase):
    """Un peer connu dans la membership view."""

    node_id: str
    display_name: str = ""
    endpoint: str = ""  # Réservé pour issue #4 (transport)
    public_key: str = ""
    joined_at: str = Field(default_factory=_now_iso)
    status: MemberStatus = MemberStatus.ACTIVE
    #: Durable per-incarnation tag (P10-3). For a Mesh-admitted member this is the
    #: ``pair_id`` that admitted it; re-admitting the same identity carries a fresh
    #: pair_id, so a retained pairing can force-evict ONLY the incarnation it
    #: activated (compare-and-evict under the membership lock). ``None`` ⇔ champ
    #: absent (member not admitted through the Mesh pending flow, byte-for-byte
    #: retro-compat). EXCLUDED from ``candidate_view_digest`` (n'affecte pas la
    #: convergence).
    incarnation: Optional[str] = None
    # Scopes (ADR-0016) — déclaré EN DERNIER pour figer l'ordre des octets :
    # quand la clé est présente, elle s'ajoute en queue de l'objet sérialisé.
    # ``None`` ⇔ champ absent ⇔ jeu complet (rétro-compat octet-pour-octet) ;
    # un manifest qui restreint écrit une liste triée déterministe.
    scopes: Optional[list[str]] = None

    @field_validator("scopes")
    @classmethod
    def _validate_scopes(cls, v: Optional[list[Any]]) -> Optional[list[str]]:
        if v is None:
            return None  # None ⇔ champ absent ⇔ jeu complet (rétro-compat).
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
        return sorted(out)  # canonique, déterministe cross-host.

    @model_serializer(mode="wrap")
    def _serialize(self, handler: Any) -> dict[str, Any]:
        data = handler(self)
        # ``incarnation`` absent (None) ⇒ aucune clé : JSON octet-identique au
        # pré-P10-3 pour tout membre non admis via le flux Mesh pending.
        if getattr(self, "incarnation", None) is None:
            data.pop("incarnation", None)
        if self.scopes is None:
            # Membre legacy / full : JSON octet-identique au pré-P5-9
            # (aucune clé ``scopes``).
            data.pop("scopes", None)
        return data

    def effective_scopes(self) -> frozenset[str]:
        """``None`` (legacy/full) → ``FULL_PEER_SCOPES`` ; sinon le jeu fermé
        stocké. C'est le SEUL endroit qui résout l'absence en « tout »."""
        if self.scopes is None:
            # RA-3 fix : le défaut None→FULL est un shim retro-compat (ADR-0016)
            # qui échoue OUVERT (un champ `scopes` absent regagne COMMIT). On
            # alerte (une fois par node) pour qu'une absence INATTENDUE — hand-edit
            # de members.json, bug de migration/sérialiseur droppant la clé — soit
            # visible plutôt que silencieusement maximale. `assert_commit_allowed`
            # (ADR-0011) reste la porte d'autorisation de commit séparée.
            if self.node_id not in _warned_none_scopes:
                _warned_none_scopes.add(self.node_id)
                _logger.warning(
                    "Hivemind member %r sans champ 'scopes' → FULL scope "
                    "(read+propose+commit) par défaut retro-compat ADR-0016. "
                    "Vérifier que l'absence du champ est intentionnelle.",
                    self.node_id,
                )
            return FULL_PEER_SCOPES
        return frozenset(self.scopes)

    def has_scope(self, scope: "PeerScope | str") -> bool:
        """True si le membre détient ``scope`` (après résolution du défaut)."""
        sv = scope.value if isinstance(scope, PeerScope) else scope
        return sv in self.effective_scopes()


class MembershipView(_HivemindBase):
    """
    Vue de la membership pour un space.

    ``epoch`` est bumpé à chaque mutation. Un message avec ``membership_epoch``
    strictement inférieur au courant est rejeté par les receivers.
    """

    protocol_version: int = PROTOCOL_VERSION
    epoch: int = 0
    members: list[Member] = Field(default_factory=list)
    updated_at: str = Field(default_factory=_now_iso)

    @field_validator("epoch")
    @classmethod
    def _validate_epoch(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"epoch must be >= 0, received {v}")
        return v


# =============================================================================
# Santé node-local (séparée de la membership partagée)
# =============================================================================


class NodeHealth(_HivemindBase):
    """
    État de santé **node-local** persisté dans ``_hivemind/node_status.json``.

    Distinct de ``MemberStatus`` : c'est l'auto-évaluation de l'instance
    courante, jamais propagée comme vérité partagée. Un import partiel ou
    une corruption détectée laisse ``status`` à ``UNSAFE``/``RESYNC_REQUIRED``
    pour qu'un démarrage froid ne lise jamais un demi-état comme sain.

    ``observed_epoch`` / ``observed_bank_version`` mémorisent la valeur
    distante qui a déclenché un ``RESYNC_REQUIRED`` (epoch futur ou
    bank_version manquée), pour que l'opérateur (#10) sache jusqu'où resync.
    """

    protocol_version: int = PROTOCOL_VERSION
    status: HiveNodeStatus = HiveNodeStatus.DISABLED
    reason: str = ""
    observed_epoch: int = -1
    observed_bank_version: int = -1
    updated_at: str = Field(default_factory=_now_iso)


# =============================================================================
# Term — compteur monotone partagé
# =============================================================================


class TermState(_HivemindBase):
    """
    Term courant du protocole (analogue à Raft).

    Sert de fencing token : toute opération distante qui présente un ``term``
    inférieur au courant doit être rejetée.
    """

    protocol_version: int = PROTOCOL_VERSION
    term: int = 0
    updated_at: str = Field(default_factory=_now_iso)
    updated_by_node_id: str = ""

    @field_validator("term")
    @classmethod
    def _validate_term(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"term must be >= 0, received {v}")
        return v


# =============================================================================
# Token (mutex distribué pour la consolidation)
# =============================================================================


class TokenLeaseState(_HivemindBase):
    """
    Lease courante du token de consolidation pour un space.

    ``fencing_token`` doit être égal au ``term`` au moment du grant ; tout
    write subséquent doit présenter ce fencing_token, sinon il est rejeté
    par les peers comme étant issu d'un grant périmé.

    Invariant (vérifié) : si ``state in {HELD, RELEASING}``,
    ``fencing_token == term``. Pour ``state == FREE``, ``fencing_token``
    peut traîner à la valeur du dernier grant (≤ term).
    """

    protocol_version: int = PROTOCOL_VERSION
    state: TokenState = TokenState.FREE
    holder_node_id: Optional[str] = None
    term: int = 0
    fencing_token: int = 0
    granted_at: Optional[str] = None
    lease_until: Optional[str] = None  # ISO 8601 — borne sup sans renouvellement
    membership_epoch: int = 0
    # bank_version au moment du grant (utile pour corréler un commit issu de
    # ce détenteur). -1 si non applicable (ex: token FREE initial).
    bank_version: int = -1
    event_id: Optional[str] = None  # Référence l'event TOKEN_GRANTED
    request_id: Optional[str] = None

    @field_validator("fencing_token")
    @classmethod
    def _validate_fencing(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"fencing_token must be >= 0, received {v}")
        return v

    def model_post_init(self, __context: Any) -> None:  # type: ignore[override]
        # Invariant protocole : un token tenu ou en cours de release porte
        # forcément le fencing_token de son term. Sinon, un grant périmé
        # pourrait subsister silencieusement.
        active_states = {TokenState.HELD.value, TokenState.RELEASING.value}
        if self.state in active_states and self.fencing_token != self.term:
            raise ValueError(
                f"Invariant violé : state={self.state} exige fencing_token == term "
                f"(reçu fencing_token={self.fencing_token}, term={self.term})"
            )


# =============================================================================
# Queue FIFO de demandes
# =============================================================================


class QueueEntry(_HivemindBase):
    """
    Une demande de token en file d'attente.

    L'ordre FIFO est porté par ``sequence`` (zero-padded dans la clé S3).
    Les acquittements (ACKs) référencent ``event_id``.
    """

    protocol_version: int = PROTOCOL_VERSION
    event_id: str
    request_id: str = ""
    sequence: int
    requester_node_id: str
    requested_at: str = Field(default_factory=_now_iso)
    term: int = 0
    membership_epoch: int = 0
    # bank_version au moment de l'enqueue (le demandeur annonce sur quelle
    # version il prévoit de travailler). -1 si non applicable.
    bank_version: int = -1
    status: QueueEntryStatus = QueueEntryStatus.PENDING

    @field_validator("sequence")
    @classmethod
    def _validate_sequence(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"sequence must be >= 0, received {v}")
        return v


# =============================================================================
# ACKs
# =============================================================================


class Ack(_HivemindBase):
    """
    Acquittement individuel d'un peer pour un event donné.

    ``payload_hash`` permet de détecter qu'un peer ACK une version stale
    du payload (par exemple un commit replayé après réorganisation locale).
    """

    protocol_version: int = PROTOCOL_VERSION
    event_id: str
    request_id: str = ""
    ack_by_node_id: str
    ack_at: str = Field(default_factory=_now_iso)
    term: int = 0
    membership_epoch: int = 0
    # bank_version observé par le peer qui ACK (utile pour détecter qu'un
    # peer ACK une version stale du payload). -1 si non applicable.
    bank_version: int = -1
    payload_hash: str = ""


# =============================================================================
# Bank commits
# =============================================================================


class BankCommitManifestEntry(_HivemindBase):
    """Une entrée du manifest d'un commit de bank (un fichier consolidé)."""

    path: str  # Relatif à {space_id}/bank/, ex: "activeContext.md"
    sha256: str
    size: int = 0

    @field_validator("size")
    @classmethod
    def _validate_size(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"size must be >= 0, received {v}")
        return v


class BankCommit(_HivemindBase):
    """
    Commit atomique d'une nouvelle version de bank.

    ``bank_version`` est monotone croissant. ``parent_bank_version`` doit
    être ``bank_version - 1`` (à l'exception du commit initial dont le
    parent vaut ``-1``). Le pointeur courant vit dans ``bank_version.json``.
    """

    protocol_version: int = PROTOCOL_VERSION
    bank_version: int
    parent_bank_version: int = -1
    term: int
    membership_epoch: int = 0
    commit_id: str  # UUID hex stable pour idempotence
    event_id: str = ""
    request_id: str = ""
    committed_by_node_id: str
    committed_at: str = Field(default_factory=_now_iso)
    manifest: list[BankCommitManifestEntry] = Field(default_factory=list)
    notes_consumed: list[str] = Field(default_factory=list)  # IDs des notes mangées

    @field_validator("bank_version")
    @classmethod
    def _validate_bank_version(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"bank_version must be >= 0, received {v}")
        return v

    @field_validator("term")
    @classmethod
    def _validate_term(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"term must be >= 0, received {v}")
        return v


class BankVersionPointer(_HivemindBase):
    """Pointeur vers le dernier commit appliqué localement."""

    protocol_version: int = PROTOCOL_VERSION
    bank_version: int = -1  # -1 = aucun commit
    commit_id: str = ""
    updated_at: str = Field(default_factory=_now_iso)


# =============================================================================
# Tombstones (suppressions de live-notes)
# =============================================================================


class Tombstone(_HivemindBase):
    """
    Tombstone d'une live-note (suppression marquée durablement).

    Utilisé par le protocole de réplication pour éviter qu'une note
    consommée par une consolidation locale ne soit repropagée par un peer
    qui n'aurait pas encore appliqué le commit.
    """

    protocol_version: int = PROTOCOL_VERSION
    note_id: str
    deleted_at: str = Field(default_factory=_now_iso)
    deleted_by_node_id: str
    term: int = 0
    membership_epoch: int = 0
    bank_version: int = -1
    # Corrélation avec l'event protocole qui a produit la tombstone (commit
    # bank consommant la note, ou suppression manuelle administrée).
    event_id: str = ""
    request_id: str = ""
    reason: str = ""


# =============================================================================
# Watermarks (vue locale du progrès des peers)
# =============================================================================


class Watermark(_HivemindBase):
    """
    Watermark représentant la position connue d'un peer dans le flux
    d'events.

    Sémantique des champs du contrat protocole :

    - ``bank_version`` (issue #3) : la **dernière bank_version** que ce peer
      a confirmé avoir appliquée. C'est le watermark proprement dit ; on
      garde le même nom que le contrat global pour rester cohérent.
    - ``last_event_id`` + ``last_event_ts`` : permettent de redémarrer une
      réplication à partir d'un point connu sans relire tout le journal.
    - ``term`` / ``membership_epoch`` : term et epoch observés au moment de
      la dernière mise à jour de la watermark.
    - ``event_id`` / ``request_id`` : corrélateurs de l'event protocole qui
      a déclenché la mise à jour de la watermark.
    """

    protocol_version: int = PROTOCOL_VERSION
    node_id: str
    last_event_id: str = ""
    last_event_ts: str = ""
    bank_version: int = -1
    updated_at: str = Field(default_factory=_now_iso)
    term: int = 0
    membership_epoch: int = 0
    event_id: str = ""
    request_id: str = ""


# =============================================================================
# Event envelope — journal append-only
# =============================================================================


class EventEnvelope(_HivemindBase):
    """
    Enveloppe générique d'un événement Hivemind dans le journal append-only.

    Contient TOUS les champs communs demandés par l'issue #3 :
    ``protocol_version``, ``membership_epoch``, ``term``, ``bank_version``,
    ``event_id``, ``request_id``. Le ``payload`` est typé par ``type`` et
    désérialisé par le consommateur.

    ``event_id`` est la clé d'idempotence : deux enveloppes avec le même
    ``event_id`` sont équivalentes et le store déduplique avant write.
    """

    protocol_version: int = PROTOCOL_VERSION
    event_id: str
    request_id: str = ""
    type: EventType
    created_at: str = Field(default_factory=_now_iso)
    origin_node_id: str
    term: int = 0
    membership_epoch: int = 0
    bank_version: int = -1
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("event_id")
    @classmethod
    def _validate_event_id(cls, v: str) -> str:
        if not v:
            raise ValueError("event_id cannot be empty")
        if "/" in v:
            raise ValueError("event_id must not contain '/'")
        return v


# =============================================================================
# Bootstrap snapshot — export/import d'un space vers un peer vierge (issue #5)
# =============================================================================


class BootstrapManifestEntry(_HivemindBase):
    """
    Une entrée du manifest de bootstrap : un fichier partagé du space avec
    son empreinte intégrité.

    ``path`` est relatif à la racine du space (PAS au préfixe ``bank/`` comme
    ``BankCommitManifestEntry``). Exemples :
    ``"_rules.md"``, ``"_synthesis.md"``, ``"_meta.json"``,
    ``"bank/activeContext.md"``, ``"live/20260101T000000_a_obs_ab12cd34.md"``,
    ``"_hivemind/members.json"``, ``"_hivemind/commits/00..00.json"``.

    ``sha256`` couvre les octets UTF-8 EXACTS qui seront (ré)écrits côté
    cible — déterministe cross-host car le contenu sérialisé est canonique.
    """

    path: str
    sha256: str
    size: int = 0

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        if not v:
            raise ValueError("path cannot be empty")
        if v.startswith("/") or ".." in v.split("/"):
            raise ValueError(f"Invalid manifest path: {v!r}")
        return v

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, v: str) -> str:
        if not v:
            raise ValueError("sha256 cannot be empty")
        return v

    @field_validator("size")
    @classmethod
    def _validate_size(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"size must be >= 0, received {v}")
        return v


class BootstrapManifest(_HivemindBase):
    """
    Manifest versionné d'un snapshot de bootstrap (HIVEMIND.md §5.1).

    Couvre TOUS les fichiers partagés du space source (bank, rules,
    synthesis, projection partagée de ``_meta.json``, live notes, et l'état
    Hivemind node-indépendant : members/term/bank_version/commits/...).

    ``manifest_sha256`` est calculé sur le JSON canonique des paires
    ``(path, sha256)`` triées — il rend détectable toute troncature ou
    réordonnancement du manifest lui-même (un attaquant qui supprime une
    entrée casse ce hash). L'import recalcule chaque empreinte par-fichier
    ET ce hash de manifest avant d'écrire le moindre objet.

    ``membership_epoch`` et ``bank_version`` portent l'état source : l'import
    les préserve tels quels (il ne réinitialise PAS à epoch 0 / version -1).
    """

    protocol_version: int = PROTOCOL_VERSION
    source_node_id: str
    membership_epoch: int = 0
    bank_version: int = -1
    commit_id: str = ""
    created_at: str = Field(default_factory=_now_iso)
    entries: list[BootstrapManifestEntry] = Field(default_factory=list)
    manifest_sha256: str = ""

    @field_validator("membership_epoch")
    @classmethod
    def _validate_epoch(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"membership_epoch must be >= 0, received {v}")
        return v

    @field_validator("bank_version")
    @classmethod
    def _validate_bank_version(cls, v: int) -> int:
        if v < -1:
            raise ValueError(f"bank_version must be >= -1, received {v}")
        return v


# =============================================================================
# Snapshot — vue complète d'un space pour rechargement
# =============================================================================


class HivemindStateSnapshot(_HivemindBase):
    """
    Vue agrégée de l'état d'un space, retournée par
    ``HivemindStateStore.load_snapshot``.

    Utilisée pour le redémarrage froid : reconstruit la vue protocole
    complète sans avoir à appeler les helpers granulaires.
    """

    space_id: str
    protocol_version: int = PROTOCOL_VERSION
    node: Optional[NodeIdentity] = None
    # Santé node-local : DOIT figurer dans la vue de cold-start, sinon un
    # restart après import échoué / resync raterait l'état UNSAFE/RESYNC_REQUIRED
    # et lirait le reste de l'état protocole comme valide.
    node_status: Optional[NodeHealth] = None
    membership: Optional[MembershipView] = None
    term: Optional[TermState] = None
    token: Optional[TokenLeaseState] = None
    bank_version_pointer: Optional[BankVersionPointer] = None
    queue: list[QueueEntry] = Field(default_factory=list)
    commits: list[BankCommit] = Field(default_factory=list)
    tombstones: list[Tombstone] = Field(default_factory=list)
    watermarks: list[Watermark] = Field(default_factory=list)
    known_event_count: int = 0
