# -*- coding: utf-8 -*-
"""
Runtime de lease/term/fencing du token de consolidation Hivemind (issue #13 /
ADR-0011 + ADR-0009).

Ce module porte le **point d'autorisation UNIQUE** d'un commit partagé :
``assert_commit_allowed()`` (ADR-0011 §Decision Outcome). C'est le SEUL chemin
qui autorise un ``BANK_COMMIT`` partagé ; aucun caller ne contourne le prédicat
en lisant un booléen mou. Toute défaillance lève un ``CommitNotAuthorized`` typé
avec un code de raison stable (``not_holder`` / ``stale_term`` / ``fenced`` /
``version_conflict`` / ``blocked``).

Deux couches, à l'image de ``queue_runtime`` :

- **Couche pure** (``compute_lease_until`` / ``is_lease_expired`` /
  ``lease_is_active`` / ``evaluate_commit_authorization`` + ``CommitIntent`` /
  ``CommitNotAuthorized`` / ``CommitDenyReason``) : aucune I/O, aucune mutation,
  aucune horloge murale (l'instant est passé en argument). Le SEUL ``try/except``
  toléré ici NE MASQUE jamais une corruption : il NORMALISE un ``lease_until``
  non parsable en ``CorruptedStateError`` (taxonomie d'erreur critique), il ne
  rattrape ni n'avale aucun ``CorruptedStateError``. C'est cette couche que le
  pair importe verbatim pour re-dériver INDÉPENDAMMENT le même prédicat à la
  réception d'un ``BANK_COMMIT``.
- **Couche async** (``LeaseRuntime``) : un wrapper mince autour du store. Elle
  charge l'état (``get_token`` / ``get_term`` / ``get_bank_version_pointer``),
  demande le head à la QUEUE (``QueueRuntime.head`` — ADR-0009 : la lease ne
  ré-dérive JAMAIS l'ordre) et délègue tout le prédicat à la couche pure. La
  garantie fail-closed vit ENTIÈREMENT dans le fait de NE PAS rattraper
  ``CorruptedStateError`` au site d'appel du store — aucun ``try/except`` dans la
  couche async ne masque une corruption.

Invariants protocole portés (ADR-0011) :

- full-mesh all-ACK (IDENTITÉ d'ensemble via ``QueueRuntime.is_fully_acked``,
  PAS un quorum/compte) ;
- pas de master central : l'autorisation est recalculée PAR COMMIT depuis le
  ``term.json`` / ``token.json`` vivants, jamais un privilège permanent ;
- pas de mémoire longue / graph dans le chemin de validité du commit (ce module
  n'importe ni ``graph_push`` ni ``consolidation_queue`` / ``consolidator`` /
  ``long``) ;
- pas de défédération par timer : l'horloge ne gouverne QUE l'expiration de la
  lease (un input d'autorisation par-commit), jamais la membership ;
- fail-closed sur corruption (``CorruptedStateError`` propage) ;
- exactement UN holder HELD par term (le second claimer en split-brain est
  fencé), garanti par la garde d'exclusion mutuelle + l'invariant modèle
  ``fencing_token == term`` + la dual-monotonie de ``set_token``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from ..reservation_guard import assert_no_pairing_activation, assert_space_not_reserved
from .models import (
    BankVersionPointer,
    CorruptedStateError,
    MembershipView,
    QueueEntryStatus,
    TermState,
    TokenLeaseState,
    TokenState,
)
from .queue_runtime import QueueRuntime
from .state import HivemindStateStore


# =============================================================================
# Constantes et type d'horloge injectable
# =============================================================================

#: TTL de lease par défaut (secondes). Aligné sur
#: ``peer.DEFAULT_REPLAY_WINDOW_SECONDS`` (300) — la même fenêtre logique borne
#: la fraîcheur d'un message et la validité d'une lease.
LEASE_TTL_SECONDS_DEFAULT = 300

#: Horloge injectable : un ``Callable`` sans argument retournant un ``datetime``
#: aware UTC. En production : ``_now_utc`` ; en test : ``DeterministicClock``.
Clock = Callable[[], datetime]


def _now_utc() -> datetime:
    """Instant courant UTC (aware). Seule lecture d'horloge murale du module,
    injectable via le seam ``clock=`` de ``LeaseRuntime``."""
    return datetime.now(timezone.utc)


def _parse_lease_until(value: str) -> datetime:
    """Parse un ``lease_until`` ISO-8601 en ``datetime`` aware UTC (mêmes règles
    que ``model._parse`` / ``peer``) : un timestamp naïf est interprété UTC.

    Un ``lease_until`` non parsable (chaîne malformée, type inattendu) est un
    état critique CORROMPU, pas un refus « normal » : on le route en
    ``CorruptedStateError`` (fail-closed, taxonomie d'erreur critique d'ADR-0011)
    plutôt que de laisser fuir un ``ValueError``/``TypeError`` nu qu'un caller
    gérant la corruption Hivemind ne reconnaîtrait pas comme unsafe/resync."""
    try:
        p = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CorruptedStateError(
            f"unparseable lease_until (corrupt token state): {value!r}"
        ) from exc
    return p if p.tzinfo else p.replace(tzinfo=timezone.utc)


# =============================================================================
# Couche pure — helpers de lease (sans I/O, sans mutation, sans try/except)
# =============================================================================


def compute_lease_until(now: datetime, ttl_seconds: int) -> str:
    """
    ``now`` (microseconde tronquée) + ``ttl_seconds``, sérialisé ISO-8601.

    La troncature des microsecondes garantit une ré-écriture byte-identique de
    la lease (alignée sur ``model.grant``). Lève ``ValueError`` si
    ``ttl_seconds <= 0`` (une lease à TTL nul/négatif serait expirée d'emblée).
    """
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be > 0, received {ttl_seconds}")
    base = now.replace(microsecond=0)
    return (base + timedelta(seconds=ttl_seconds)).isoformat()


def is_lease_expired(token: TokenLeaseState, now: datetime) -> bool:
    """
    Vrai si la lease a expiré à l'instant ``now``.

    Fail-closed sur état critique incomplet (ADR-0011) : un token ACTIF
    (``state in {HELD, RELEASING}``) DOIT porter un ``lease_until`` valide. Un
    actif sans ``lease_until`` (``None``) est un état critique CORROMPU — JAMAIS
    « jamais expiré / valide à vie » — et lève ``CorruptedStateError``. Sans
    cela, un HELD sans borne de lease autoriserait un commit partagé
    indéfiniment (fail-open). Idem pour un ``lease_until`` malformé non parsable
    (cf. ``_parse_lease_until``).

    Pour un token NON actif (FREE), ``lease_until is None`` reste bénin (``False``,
    rien à expirer : ce n'est pas une lease vivante). Sinon comparaison STRICTE
    ``now > lease_until`` (demi-ouverte) : exactement à ``lease_until``, la lease
    est ENCORE valide. Prend un ``datetime`` (l'instant de l'horloge du runtime),
    pas une chaîne — la comparaison se fait sur des ``datetime`` parsés, jamais
    sur des chaînes ISO (offsets divergents).
    """
    is_active_state = token.state in (
        TokenState.HELD.value,
        TokenState.RELEASING.value,
    )
    # Fail-closed sur état critique incomplet (ADR-0011) : un token ACTIF
    # (HELD/RELEASING) DOIT porter une IDENTITÉ de holder. Un actif sans
    # holder_node_id (None / chaîne vide) est aussi corrompu qu'un actif sans
    # lease_until : le modèle DÉFINIT HELD comme « un nœud tient le token »
    # (models.py §TokenState), mais autorise structurellement
    # holder_node_id=None — un actif holderless est donc un état critique
    # incomplet, jamais une lease vivante anonyme. On le route en
    # CorruptedStateError (resync requis), pas en « valide ». Vérifié AVANT le
    # lease_until pour que la corruption d'identité remonte même si la borne de
    # lease est présente et bien formée.
    if is_active_state and not token.holder_node_id:
        raise CorruptedStateError(
            "active token without holder_node_id (incomplete critical state): "
            f"state={token.state}, holder={token.holder_node_id!r}, "
            f"term={token.term} — a HELD/RELEASING token MUST carry a holder "
            "identity; fail-closed, never an anonymous live lease"
        )
    if token.lease_until is None:
        if is_active_state:
            raise CorruptedStateError(
                "active token without lease_until (incomplete critical state): "
                f"state={token.state}, holder={token.holder_node_id!r}, "
                f"term={token.term} — a HELD/RELEASING token MUST carry a valid "
                "lease bound; fail-closed, never valid forever"
            )
        return False
    return now > _parse_lease_until(token.lease_until)


def lease_is_active(token: TokenLeaseState | None, now: datetime) -> bool:
    """
    Vrai si « quelqu'un tient une lease vivante à l'instant ``now`` » :
    ``token`` non nul ET ``state in {HELD, RELEASING}`` ET lease non expirée.

    On compare aux valeurs CHAÎNES de l'enum (``use_enum_values=True`` sur le
    modèle : ``token.state`` est déjà une chaîne).
    """
    if token is None:
        return False
    if token.state not in (TokenState.HELD.value, TokenState.RELEASING.value):
        return False
    return not is_lease_expired(token, now)


def assert_active_lease_structural(token: TokenLeaseState, now: datetime) -> None:
    """
    Garde fail-closed UNIQUE de la structure de lease d'un token ACTIF, partagée
    par TOUS les chemins qui acceptent / effacent / préservent un token
    HELD/RELEASING (``release`` / ``reconcile_stale_holder`` en plus des gates
    acquire/renew/assert qui passent déjà par ``is_lease_expired``).

    Délègue à ``is_lease_expired`` (l'unique site de validité de lease) et JETTE
    le booléen : on ne veut pas l'expiration ici, seulement l'effet de bord
    fail-closed. Un token ACTIF (``state in {HELD, RELEASING}``) dont le
    ``lease_until`` est ``None``/malformé OU dont le ``holder_node_id`` est
    manquant (``None``/chaîne vide) est un état critique CORROMPU et
    ``is_lease_expired`` lève ``CorruptedStateError`` AVANT toute comparaison à
    ``now`` (la détection de corruption est indépendante de l'horloge : un holder
    absent ou un ``lease_until`` ``None`` sur un actif lève d'emblée, un
    ``lease_until`` non parsable lève au parse). Un actif holderless n'est PAS une
    lease vivante anonyme : ``HELD`` DÉFINIT « un nœud tient le token » (models.py),
    donc un holder manquant est un incomplet critique, jamais réparable en FREE.
    Pour un token NON actif (FREE) c'est un no-op (rien à valider).

    Sans cette garde, ``release()`` (un holder qui matche) ou
    ``reconcile_stale_holder()` (un holder au term courant) pourraient
    silencieusement accepter/réparer un ``HELD(lease_until=None)`` corrompu en le
    transformant en ``FREE`` ou en le préservant comme légitime — un fail-OPEN
    qui efface l'état critique corrompu au lieu de le faire remonter (resync
    requis). ``release()`` et ``reconcile_stale_holder()`` ne RÉPARENT jamais une
    corruption critique ; ils échouent fermés.
    """
    if token.state in (TokenState.HELD.value, TokenState.RELEASING.value):
        # Effet de bord seul : is_lease_expired lève CorruptedStateError sur un
        # actif corrompu (lease_until None/malformé). On ignore le bool retourné.
        is_lease_expired(token, now)


def assert_active_token_term_consistent(
    token: TokenLeaseState, term: TermState | None
) -> None:
    """
    Garde fail-closed de cohérence TERM d'un token ACTIF (HELD/RELEASING) contre le
    ``term.json`` vivant, partagée par ``renew`` / ``release`` /
    ``reconcile_stale_holder`` (Codex BLOCKING head 20e2e5b). Jumelle de
    ``assert_active_lease_structural`` pour la dimension term.

    Un token actif IMPLIQUE qu'un grant a eu lieu, et ``acquire`` bumpe TOUJOURS le
    ``term.json`` AVANT d'écrire le token à ``term == new_term``. Donc pour un actif
    sain, ``term.json`` est présent et ``token.term <= term.term``. Deux états sont
    IMPOSSIBLES en flux normal et constituent une CORRUPTION critique :

      - ``term.json`` absent (``None``) sous un token actif -> état incomplet ;
      - ``token.term > term.term`` (token « au futur ») -> le term aurait régressé
        (impossible, monotone) ou un token aurait été écrit sans bump.

    Les deux lèvent ``CorruptedStateError`` (resync requis) AVANT toute décision de
    renew / keep / demote / erase, jamais traités comme un refus « normal » ni
    défaltés à 0 (ce qui rendrait un token term-0 sans ``term.json`` faussement
    légitime, ou préserverait/effacerait un token au futur). Un ``token.term <
    term.term`` (holder SUPERSEDED) n'est PAS une corruption ici : c'est un stale
    normal, géré en aval (``STALE_TERM`` côté renew, démotion côté reconcile). No-op
    sur un token NON actif (FREE).
    """
    if token.state not in (TokenState.HELD.value, TokenState.RELEASING.value):
        return
    if term is None:
        raise CorruptedStateError(
            "active token without live term.json (incomplete critical state): "
            f"state={token.state}, holder={token.holder_node_id!r}, "
            f"token.term={token.term} — a HELD/RELEASING token implies a grant that "
            "bumped the term; fail-closed, never default to 0"
        )
    if token.term > term.term:
        raise CorruptedStateError(
            f"active token at term {token.term} > term.json {term.term} "
            "(impossible: acquire bumps the term BEFORE writing the token) — "
            "fail-closed, never preserved/renewed/cleared as legitimate"
        )


# =============================================================================
# Couche pure — prédicat d'autorisation de commit (ADR-0011)
# =============================================================================


class CommitDenyReason(str, Enum):
    """Codes de refus stables d'``assert_commit_allowed`` (figés par ADR-0011).

    ``str``-Enum pour rester aligné sur la convention du codebase
    (``TokenState`` / ``HiveNodeStatus`` / ``WriteRoute``) : sérialisable et
    comparable à sa chaîne.
    """

    NOT_HOLDER = "not_holder"
    STALE_TERM = "stale_term"
    FENCED = "fenced"
    VERSION_CONFLICT = "version_conflict"
    BLOCKED = "blocked"


class CommitNotAuthorized(RuntimeError):
    """
    Levée par le point d'autorisation unique quand un commit partagé est REFUSÉ.

    Porte un ``reason`` (``CommitDenyReason``) machine-lisible et des
    ``details`` optionnels. JAMAIS un booléen mou qu'un caller pourrait ignorer.

    ``CorruptedStateError`` n'est JAMAIS mappé sur un ``CommitNotAuthorized`` :
    une corruption propage telle quelle (fail-closed), elle n'est pas un refus
    « normal » avec code de raison.
    """

    def __init__(
        self,
        reason: CommitDenyReason,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Forme filaire uniforme avec ``PeerChannelError.to_dict`` (peer.py)."""
        return {
            "status": "error",
            "reason": self.reason.value,
            "message": str(self),
            "details": self.details,
        }


@dataclass(frozen=True)
class CommitIntent:
    """
    Auto-déclaration d'un holder qui propose un ``BANK_COMMIT`` partagé.

    Le gate ne FAIT CONFIANCE à rien ici : ``CommitIntent`` est délibérément un
    ``dataclass`` (pas un modèle Pydantic) SANS validation d'invariant. Baker
    ``fencing_token == term`` dans le constructeur rendrait IMPOSSIBLE de
    construire l'intent adverse (holder stale / re-dérivation du pair) que le
    prédicat doit précisément REJETER. C'est le gate qui valide l'égalité
    term/fencing, pas l'intent.

    Champs = exactement les inputs du prédicat ADR-0011 (rien de plus). La
    liaison au space est déjà garantie : ``LeaseRuntime`` est construit par-store
    avec un ``space_id`` cohérent (garde constructeur).
    """

    holder_node_id: str  # l'identité du holder qui s'auto-déclare
    term: int  # le term que le holder croit tenir
    fencing_token: int  # le fencing revendiqué (le gate vérifie == term)
    bank_version: int  # la nouvelle version proposée
    previous_bank_version: int  # le parent calculé par le holder (CAS)
    commit_id: str


def evaluate_commit_authorization(
    *,
    token: TokenLeaseState | None,
    term: TermState | None,
    pointer: BankVersionPointer | None,
    intent: CommitIntent,
    now: datetime,
) -> None:
    """
    LE prédicat pur d'autorisation de commit (ADR-0011). Retourne ``None`` ssi
    autorisé ; sinon lève ``CommitNotAuthorized`` avec une raison stable.

    AUCUNE I/O, AUCUN ``try/except``, AUCUNE mutation. C'est la MÊME fonction que
    le pair appelle à la réception d'un ``BANK_COMMIT`` contre SON propre état
    chargé (writer local + validateur distant = un seul prédicat, deux sites
    d'appel).

    Ordre d'évaluation (refus au PREMIER échec ; l'ordre est porteur — cf. le
    trou expiré-mais-non-superseded ci-dessous) :

      0a. CORRUPTION      — token ACTIF (HELD/RELEASING) corrompu ->
                            ``CorruptedStateError`` PROPAGE, AVANT tout refus
                            « normal » ET avant le BLOCKED de l'étape 0b. Deux
                            dimensions : structure de lease (``lease_until``
                            None/malformé OU ``holder_node_id`` manquant,
                            ``assert_active_lease_structural``) ET cohérence term
                            (``term.json`` absent sous un actif OU token au futur
                            ``token.term > term.term``,
                            ``assert_active_token_term_consistent``). La corruption
                            d'un token actif ne doit JAMAIS être masquée en
                            NOT_HOLDER/STALE_TERM ni en BLOCKED « absent » (un actif
                            sans term.json EST une corruption, pas un état non
                            initialisé). No-op sur un actif sain ou un FREE ; ne
                            JETTE que sur corruption, pas sur expiration (cf. FENCED).
      0b. BLOCKED         — ``token`` / ``term`` / ``pointer`` GÉNUINEMENT absent
                            (non initialisé) -> refus fail-closed, jamais
                            default-allow. (Un actif sans term.json a déjà remonté
                            en corruption en 0a.)
      1. NOT_HOLDER       — token non HELD, OU holder != asserter. Un token FREE /
                            RELEASING / tenu par un autre n'autorise pas un commit
                            frais.
      2. STALE_TERM       — chaîne d'égalité NON satisfaite :
                            ``token.term == token.fencing_token == term.term
                            == intent.term == intent.fencing_token``. Un holder
                            superseded (``token.term < term.term``) tombe ici (le
                            term a bougé). Le modèle force déjà
                            ``token.fencing_token == token.term`` pour HELD ; on le
                            ré-assert contre le term VIVANT, on ne le ré-dérive
                            jamais.
      3. FENCED           — lease expirée à ``now``. Holder au term COURANT
                            (``token.term == term.term``, donc l'étape 2 passe)
                            mais dont la lease a élapsé -> fencé. Un holder stale
                            qui revient n'est JAMAIS ré-autorisé (HIVEMIND.md
                            §6.2). SI cette étape était omise, un holder expiré
                            mais non encore superseded serait autorisé à tort.
                            ``is_lease_expired`` est ici fail-closed : un HELD
                            sans ``lease_until`` (ou un ``lease_until`` malformé)
                            est un état critique incomplet -> ``CorruptedStateError``
                            PROPAGE (jamais traité « valide à vie »), il n'est pas
                            mappé sur un ``CommitNotAuthorized``.
      4. VERSION_CONFLICT — ``intent.previous_bank_version != pointer.bank_version``
                            (CAS atomique contre le pointeur ``bank_version.json``
                            vivant).
    """
    # 0a. Token actif CORROMPU (fail-closed AVANT tout refus « normal » ET avant le
    # BLOCKED générique). Un token ACTIF (HELD/RELEASING) corrompu DOIT remonter en
    # CorruptedStateError, jamais classé en refus ordinaire ni en BLOCKED. Deux
    # dimensions :
    #   - structure de lease : lease_until None/malformé OU holder_node_id manquant
    #     (``assert_active_lease_structural``) ;
    #   - cohérence term : term.json ABSENT sous un actif OU token AU FUTUR
    #     (``token.term > term.term``) (``assert_active_token_term_consistent``).
    # Faire ces gardes AVANT le BLOCKED de l'étape 0b garantit qu'un actif sans
    # term.json remonte comme CORRUPTION (cohérent avec acquire/renew/release/
    # reconcile), et non comme un BLOCKED « état absent » ordinaire (Codex MEDIUM
    # head a0c51c2). Sans ces checks en TÊTE, un HELD corrompu tenu par nodeB face
    # à un intent de nodeA tomberait en NOT_HOLDER (étape 1) ou en BLOCKED, masquant
    # la corruption. No-op sur un token FREE ou un actif sain ; l'expiration (lease
    # saine mais élapsée) reste classée FENCED à l'étape 3 — ces checks ne JETTENT
    # que sur corruption, pas sur expiration. Un ``token.term < term.term``
    # superseded reste un STALE_TERM normal (étape 2), pas une corruption.
    if token is not None:
        assert_active_lease_structural(token, now)
        assert_active_token_term_consistent(token, term)

    # 0b. État critique GÉNUINEMENT absent (non initialisé) ? -> BLOCKED fail-closed,
    # jamais default-allow. Un token actif sans term.json a déjà remonté en
    # corruption en 0a ; ici on bloque l'état réellement absent (pas de token, ou
    # FREE/None + term/pointer non initialisés).
    if token is None or term is None or pointer is None:
        raise CommitNotAuthorized(
            CommitDenyReason.BLOCKED,
            "commit refused: critical protocol state is absent "
            f"(token={token is not None}, term={term is not None}, "
            f"bank_version={pointer is not None}) — fail-closed, never "
            "default-allow",
            {
                "has_token": token is not None,
                "has_term": term is not None,
                "has_pointer": pointer is not None,
            },
        )

    # 1. Holder ? (token HELD tenu par l'asserter)
    if (
        token.state != TokenState.HELD.value
        or token.holder_node_id != intent.holder_node_id
    ):
        raise CommitNotAuthorized(
            CommitDenyReason.NOT_HOLDER,
            f"commit refused: {intent.holder_node_id!r} does not hold the token "
            f"(state={token.state}, holder={token.holder_node_id!r}) — a "
            "non-holder never authorizes a shared commit",
            {
                "token_state": token.state,
                "token_holder": token.holder_node_id,
                "asserting_holder": intent.holder_node_id,
            },
        )

    # 2. Term courant ? (chaîne d'égalité contre le term.json vivant)
    if not (
        token.term
        == token.fencing_token
        == term.term
        == intent.term
        == intent.fencing_token
    ):
        raise CommitNotAuthorized(
            CommitDenyReason.STALE_TERM,
            "commit refused: stale term/fencing — equality chain is broken "
            f"(token.term={token.term}, token.fencing={token.fencing_token}, "
            f"term.json={term.term}, intent.term={intent.term}, "
            f"intent.fencing={intent.fencing_token}) — holder superseded, "
            "never re-authorized",
            {
                "token_term": token.term,
                "token_fencing": token.fencing_token,
                "current_term": term.term,
                "intent_term": intent.term,
                "intent_fencing": intent.fencing_token,
            },
        )

    # 3. Lease vivante ? (expiry par l'horloge — le trou expiré-non-superseded)
    if is_lease_expired(token, now):
        raise CommitNotAuthorized(
            CommitDenyReason.FENCED,
            "commit refused: lease expired at clock time "
            f"(lease_until={token.lease_until!r}, now={now.isoformat()}) — "
            "expired holder fenced, never automatically re-authorized (HIVEMIND.md §6.2)",
            {
                "lease_until": token.lease_until,
                "now": now.isoformat(),
                "term": token.term,
            },
        )

    # 4. Parent attendu ? (CAS atomique contre le pointeur vivant)
    if intent.previous_bank_version != pointer.bank_version:
        raise CommitNotAuthorized(
            CommitDenyReason.VERSION_CONFLICT,
            "commit refused: parent bank_version diverges "
            f"(intent.previous={intent.previous_bank_version} != "
            f"pointer={pointer.bank_version}) — lost update is forbidden (CAS)",
            {
                "intent_previous_bank_version": intent.previous_bank_version,
                "current_bank_version": pointer.bank_version,
            },
        )


# =============================================================================
# Couche async — surface store-facing (issue #13)
# =============================================================================

# Verrou de MUTATION DU TOKEN PAR-SPACE (issue #13 — atomicité read-modify-write).
#
# TOUTES les méthodes qui font un read-modify-write de ``token.json``
# (``acquire`` / ``renew`` / ``release`` / ``reconcile_stale_holder``) lisent
# l'état PUIS, après plusieurs ``await``, écrivent — SANS CAS atomique sur le
# store. Sans sérialisation IN-PROCESS, deux de ces méthodes concurrentes
# s'entrelacent dans le trou check-then-act et violent l'exclusion mutuelle
# single-HELD. Exemples : (a) deux ``acquire`` accordent un SECOND holder
# (split-brain G3) ; (b) un ``renew`` qui rend une lease vivante pendant qu'un
# ``acquire`` lit le snapshot PÉRIMÉ d'avant-renew, passe G3 et s'auto-accorde
# par-dessus la lease renouvelée (Codex HIGH head fb6f112). On sérialise donc le
# read-modify-write COMPLET de CHACUNE de ces méthodes sous un MÊME verrou
# par-space, à l'image de ``MembershipService._space_lock`` (lifecycle.py). C'est
# précisément cette sérialisation qui rend valide l'hypothèse d'``_acquire_locked``
# « rien ne ré-écrit le token entre le top-read et G3 » : aucune autre méthode
# mutante ne peut écrire tant qu'``acquire`` tient le verrou.
#
# Ce verrou couvre la concurrence INTRA-PROCESS uniquement. La recouvrabilité
# DURABLE (crash / erreur store en cours de séquence) est portée séparément par
# l'ORDRE des effets d'``acquire`` (mark_granted en DERNIER) + le fast-path de
# reprise idempotente (never-orphan). La sérialisation distribuée (cross-pair)
# reste portée par le term/fencing + l'exclusion mutuelle ; ce verrou protège
# l'atomicité LOCALE d'un même pair.
#
# space_id -> (event_loop, lock). Le verrou est lié à la boucle courante et
# recréé si la boucle a changé : un ``asyncio.Lock`` est attaché à sa boucle, et
# le réutiliser depuis une autre boucle lèverait. En production (boucle unique
# longue) c'est un verrou par-space stable ; en test (une boucle par test) il est
# recréé proprement.
_TOKEN_LOCKS: dict[str, tuple[Any, asyncio.Lock]] = {}


def _token_lock(space_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _TOKEN_LOCKS.get(space_id)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _TOKEN_LOCKS[space_id] = (loop, lock)
        return lock
    return entry[1]


def token_mutation_lock(space_id: str) -> asyncio.Lock:
    """Shared per-space lock for operations that must freeze term/token state."""

    return _token_lock(space_id)


class LeaseRuntime:
    """
    Wrapper async mince autour du ``HivemindStateStore`` pour la lease du token.

    Possède la machine à états acquire/renew/release/reconcile ET le point
    d'autorisation unique ``assert_commit_allowed``. Réutilise les primitives du
    store (``get_token`` / ``set_token`` dual-monotone, ``get_term`` /
    ``bump_term`` monotone, ``get_bank_version_pointer``) et demande le head à
    ``QueueRuntime`` (ADR-0009 : la lease ne ré-dérive jamais l'ordre).

    Fail-closed : aucune méthode ne rattrape ``CorruptedStateError`` — un état
    corrompu propage et BLOQUE (jamais skippé, jamais deviné, jamais converti en
    refus à code de raison).

    L'horloge est lue UNE FOIS par appel public (``now = self._clock()``) et
    threadée dans les helpers purs — jamais re-lue en milieu de méthode
    (déterminisme).
    """

    def __init__(
        self,
        store: HivemindStateStore,
        space_id: str,
        queue: QueueRuntime,
        *,
        clock: Clock = _now_utc,
        ttl_seconds: int = LEASE_TTL_SECONDS_DEFAULT,
    ) -> None:
        if space_id != store.space_id:
            raise ValueError(
                f"LeaseRuntime space_id={space_id!r} != "
                f"store.space_id={store.space_id!r}"
            )
        if space_id != queue.space_id:
            raise ValueError(
                f"LeaseRuntime space_id={space_id!r} != "
                f"queue.space_id={queue.space_id!r}"
            )
        if ttl_seconds <= 0:
            raise ValueError(f"ttl_seconds must be > 0, received {ttl_seconds}")
        self._store = store
        self._space_id = space_id
        self._queue = queue
        self._clock = clock
        self._ttl_seconds = ttl_seconds

    @property
    def space_id(self) -> str:
        return self._space_id

    async def _assert_mesh_mutation_allowed(self) -> None:
        """Fence ordinary lease/term mutations during Mesh enrollment.

        A source's e+1 snapshot binds its exact term and token.  Once a target
        is pending, any new grant, renewal, release, or stale-holder repair
        would create state that the target cannot have all-ACKed.  Run this
        check under the token mutation lock at every mutating entry point so it
        linearizes with the source final-ACK fence.  The core checkers are
        no-ops when Mesh is disabled.
        """

        await assert_space_not_reserved(self._space_id)
        await assert_no_pairing_activation(self._space_id)

    # ─────────────────────────────────────────────────────────────────
    # acquire — grant unique (bump term, token HELD, lease) sous all-ACK
    # ─────────────────────────────────────────────────────────────────

    async def acquire(
        self,
        *,
        membership: MembershipView | None,
        holder_node_id: str,
        event_id: str,
        request_id: str = "",
    ) -> TokenLeaseState:
        """
        Accorde le token au HEAD de queue SOUS all-ACK strict.

        Gardes (dans l'ordre EXACT du modèle de référence ``grant``), AUCUN effet
        de bord avant que toutes passent :

        - **G1 all-ACK** : ``QueueRuntime.is_fully_acked`` (IDENTITÉ d'ensemble
          sur l'ACTIVE, jamais un compte). Un membre actif non-ACKeur -> refus
          ``BLOCKED``. C'est ici que le non-goal quorum est protégé.
        - **G2 head-of-queue + PENDING** : ``QueueRuntime.head`` (la lease DEMANDE
          le head à la queue — ADR-0009, ne ré-dérive jamais l'ordre). Refus
          ``BLOCKED`` si pas de head, head != event_id, ou requester du head !=
          holder. ``head`` filtre déjà PENDING+ACTIVE, donc une entrée consommée
          / non-PENDING n'est simplement pas le head. Lecture seule, AVANT
          l'exclusion mutuelle pour qu'un re-grant d'une entrée déjà accordée
          échoue sur sa cause première.
        - **G3 exclusion mutuelle** : une lease HELD/RELEASING vivante détenue par
          QUICONQUE (y compris le holder lui-même) interdit un second grant ->
          refus ``BLOCKED``. C'est la garde single-HELD : le second claimer en
          split-brain est fencé ICI. Une lease expirée ou un token FREE laisse
          passer.

        Effets (seulement si G1-G3 passent), dans cet ordre — la consommation
        IRRÉVERSIBLE de la head (``mark_granted``) est le DERNIER effet
        (never-drop/never-orphan) : ``bump_term`` depuis ``get_term().term + 1``
        (jamais un compteur externe) -> ``set_token`` HELD (``fencing_token ==
        new_term`` + ``lease_until`` borné par l'horloge, le POINT DE COMMIT du
        grant) -> ``mark_granted`` (consume-once de la head). La dual-monotonie de
        ``set_token`` rejette toute régression term/fencing (un write concurrent
        stale ne peut pas écraser).

        **Atomicité & recouvrabilité (issue #13)** — deux protections distinctes
        d'un read-modify-write SANS CAS atomique sur trois objets durables
        (``term.json`` / ``token.json`` / entrée queue), non transactionnels :

        - (a) Concurrence IN-PROCESS : TOUTE la section critique (G1+G2+G3 +
          effets) tourne sous le verrou par-space (``_token_lock``) — sans lui,
          un second ``acquire`` concurrent s'intercalerait dans le trou
          check-then-act et accorderait un SECOND holder (split-brain G3).
        - (b) Recouvrabilité DURABLE (crash / erreur store EN COURS de séquence) :
          en plaçant la consommation de la head EN DERNIER, toute panne AVANT
          ``mark_granted`` laisse la head ENCORE PENDING (retryable, JAMAIS
          orpheline) au lieu de consommer la requête sans établir de holder.
          Une panne entre ``set_token`` et ``mark_granted`` laisse un token HELD
          durable pour CE ``event_id`` : le FAST-PATH de reprise idempotente en
          tête de ``_acquire_locked`` le détecte (le token HELD vivant EST la
          preuve du grant), finalise ``mark_granted`` et retourne le token — sans
          re-bumper le term ni buter sur G3.
        """
        async with _token_lock(self._space_id):
            await self._assert_mesh_mutation_allowed()
            return await self._acquire_locked(
                membership=membership,
                holder_node_id=holder_node_id,
                event_id=event_id,
                request_id=request_id,
            )

    async def _acquire_locked(
        self,
        *,
        membership: MembershipView | None,
        holder_node_id: str,
        event_id: str,
        request_id: str,
    ) -> TokenLeaseState:
        """Corps d'``acquire`` exécuté SOUS le verrou par-space (atomicité
        check-then-act). Voir ``acquire`` pour les gardes et l'invariant."""
        now = self._clock()

        # --- Corruption-first : tout token ACTIF corrompu fail-closed AVANT tout ---
        # (Codex MEDIUM head 2a9fc3d.) UNIQUE point de garde de corruption de token
        # actif d'``acquire``. Un token ACTIF (HELD/RELEASING) corrompu — structure
        # de lease (lease_until None/malformé OU holder manquant) OU cohérence term
        # (term.json absent sous un actif OU token au futur token.term > term.json) —
        # DOIT remonter en CorruptedStateError ICI, AVANT la reprise, G1 (all-ACK),
        # G2 (head) et G3, INDÉPENDAMMENT du holder/event/expiration/ACK/head. Sans ce
        # point unique en tête, un actif corrompu mais EXPIRÉ (ou tenu par un autre,
        # ou pour un autre event) sauterait la garde de reprise, et un G1/G2 en échec
        # retournerait un BLOCKED ordinaire MASQUANT la corruption critique en simple
        # attente de queue/ACK. La reprise et G3 s'appuient sur cette garde : sous le
        # verrou par-space rien ne ré-écrit le token entre ici et G3, donc ``held``
        # est le token courant tout du long. No-op si FREE / None / actif sain.
        held = await self._store.get_token()
        term_state = await self._store.get_term()
        if held is not None:
            assert_active_lease_structural(held, now)
            assert_active_token_term_consistent(held, term_state)

        # --- Fast-path : reprise idempotente d'un grant à moitié appliqué ---
        # (issue #13, recouvrabilité durable — Codex BLOCKING head 62e71dbc.)
        # Si un acquire ANTÉRIEUR pour CE event_id par CE holder a déjà écrit son
        # token HELD (bump_term + set_token réussis) mais a échoué/crashé AVANT la
        # consommation de la head (mark_granted, désormais le DERNIER effet), le
        # token HELD durable EST la preuve positive que le grant a eu lieu. Re-
        # dérouler les effets gonflerait le term, et G3 (lease vivante tenue par
        # MOI-même) bloquerait le retry indéfiniment — laissant la head PENDING
        # alors qu'un holder existe déjà (la requête ne pourrait JAMAIS sortir de
        # l'éligibilité). On COMPLÈTE donc la transition de façon idempotente :
        # finaliser mark_granted et retourner le token existant.
        #
        # Strictement borné (token HELD + holder == moi + event_id == le mien +
        # lease vivante au term COURANT) : un autre claimer, un autre event, une
        # lease expirée ou un holder superseded ne prennent JAMAIS ce chemin et
        # retombent sur G1/G2/G3 (mutual-exclusion préservée). On NE re-vérifie PAS
        # G1 all-ACK ici : le token HELD durable atteste que le grant a déjà
        # franchi G1 ; re-bloquer la finalisation sur un all-ACK redevenu faux
        # (un membre ACTIVE apparu depuis) RE-créerait précisément l'orphelin
        # qu'on corrige. ``lease_is_active`` -> ``is_lease_expired`` propage
        # ``CorruptedStateError`` sur un actif corrompu (jamais masqué, fail-closed).
        if (
            held is not None
            and held.state == TokenState.HELD.value
            and held.holder_node_id == holder_node_id
            and held.event_id == event_id
            and lease_is_active(held, now)
        ):
            # ``held`` et ``term_state`` sont déjà chargés ET validés (corruption-
            # first) en tête : la reprise ne re-lit ni ne re-garde le token. Un actif
            # corrompu (term absent/futur) a déjà fail-closed avant d'arriver ici.
            current_term = term_state.term if term_state is not None else 0
            if held.term == current_term:
                # Finalisation du grant à moitié appliqué — sur PREUVE POSITIVE de
                # l'état de NOTRE propre entrée de queue, jamais sur une inférence
                # depuis l'identité de la head (Codex BLOCKING heads f1345a6 /
                # f371e05 / fb5e486 / 5225303). On ne retourne un acquire « réussi »
                # ET on ne consomme une head QUE si l'ensemble PENDING du même
                # event_id est SANS AMBIGUÏTÉ ; toute autre forme FAIL-CLOSED.
                #
                # ``pending_same_event`` = TOUTES les entrées PENDING (tout
                # requester confondu) pour ce ``event_id``. C'est la preuve positive
                # de l'état du grant ET le détecteur de doublon : un doublon
                # same-event (divergent d'un autre requester, ou seq dupliquée du
                # même requester) y apparaît, donc on ne peut pas le masquer en
                # consommant l'entrée canonique. Scan O(n) sur le snapshot queue
                # (sous le verrou, cohérent). Trois cas :
                #   (2) AUCUNE entrée PENDING same-event : mark_granted antérieur a
                #       réussi (entrée consommée) et il n'y a aucun doublon PENDING
                #       -> pur retry idempotent, rien à finaliser, retourne le token.
                #   (1) EXACTEMENT notre entrée canonique reste PENDING (un seul
                #       PENDING same-event, requester == holder) ET c'est la head :
                #       grant à moitié appliqué, finalisation sûre -> mark_granted
                #       (idempotent) et retourne le token.
                #   (3) toute autre forme (>=2 PENDING same-event = doublon divergent
                #       ou seq dupliquée ; OU notre unique PENDING n'est pas la head ;
                #       OU l'unique PENDING n'est pas le nôtre) : l'ensemble est
                #       AMBIGU. On NE consomme RIEN (never-orphan, jamais masquer un
                #       doublon) ET on NE retourne PAS un succès silencieux — sinon
                #       le holder enchaînerait assert_commit_allowed() (qui ne
                #       re-vérifie pas la queue) et committerait par-dessus une queue
                #       divergente. FAIL-CLOSED (BLOCKED) ; l'anomalie reste surfacée
                #       par ``queue_anomalies()`` (detect_event_id_duplicates).
                entries = await self._store.list_queue()
                pending_same_event = [
                    e
                    for e in entries
                    if e.event_id == event_id
                    and e.status == QueueEntryStatus.PENDING.value
                ]
                # (2) plus aucune entrée PENDING pour cet event -> consommé.
                if not pending_same_event:
                    return held
                head = await self._queue.head(membership)
                # (1) ensemble réductible à notre seule entrée canonique = la head.
                if (
                    len(pending_same_event) == 1
                    and pending_same_event[0].requester_node_id == holder_node_id
                    and head is not None
                    and head.event_id == event_id
                    and head.requester_node_id == holder_node_id
                ):
                    await self._queue.mark_granted(head)
                    return held
                # (3) ensemble PENDING same-event ambigu -> fail-closed.
                raise CommitNotAuthorized(
                    CommitDenyReason.BLOCKED,
                    "acquire (resume) refused: the PENDING set for event "
                    f"{event_id!r} cannot be reduced to the one canonical entry "
                    f"from {holder_node_id!r} "
                    f"({len(pending_same_event)} same-event PENDING entries; "
                    f"head={head.event_id if head else None!r} requested by "
                    f"{head.requester_node_id if head else None!r}) — ambiguous/"
                    "divergent queue state, fail-closed (never a silent success or "
                    "a consumption hiding a duplicate)",
                    {
                        "event_id": event_id,
                        "holder": holder_node_id,
                        "pending_same_event_count": len(pending_same_event),
                        "head_event_id": head.event_id if head else None,
                        "head_requester": (
                            head.requester_node_id if head else None
                        ),
                    },
                )

        # --- G1 all-ACK (set-identity sur l'ACTIVE) ---
        if not await self._queue.is_fully_acked(event_id, membership):
            raise CommitNotAuthorized(
                CommitDenyReason.BLOCKED,
                f"acquire refused: all-ACK is not satisfied for event {event_id!r} "
                "(an ACTIVE member has not ACKed) — V1 blocks and does not progress",
                {"event_id": event_id},
            )

        # --- G2 head-of-queue + PENDING (lecture seule, ADR-0009) ---
        head = await self._queue.head(membership)
        if head is None or head.event_id != event_id:
            raise CommitNotAuthorized(
                CommitDenyReason.BLOCKED,
                f"acquire refused: event {event_id!r} is not the queue head "
                f"(head={head.event_id if head else None!r}) — out-of-order grant "
                "is forbidden (the lease requires the queue head, ADR-0009)",
                {
                    "event_id": event_id,
                    "head_event_id": head.event_id if head else None,
                },
            )
        if head.requester_node_id != holder_node_id:
            raise CommitNotAuthorized(
                CommitDenyReason.BLOCKED,
                f"acquire refused: head {event_id!r} was requested by "
                f"{head.requester_node_id!r}, not {holder_node_id!r} — "
                "only the head requester may acquire",
                {
                    "head_requester": head.requester_node_id,
                    "asserting_holder": holder_node_id,
                },
            )

        # --- G3 exclusion mutuelle (lease active non expirée -> single HELD) ---
        # ``held`` (lu + validé corruption-first en tête) EST le token courant : sous
        # le verrou par-space rien ne l'a ré-écrit depuis. Un actif corrompu sur le
        # term (token.term > term.json, ou term.json absent) — même EXPIRÉ, donc
        # invisible à ``lease_is_active`` — a déjà fail-closed en tête, ce qui ferme
        # le trou « actif corrompu expiré écrasé par acquire ». Ici on ne décide QUE
        # l'exclusion mutuelle (une lease vivante détenue par quiconque bloque).
        if lease_is_active(held, now):
            raise CommitNotAuthorized(
                CommitDenyReason.BLOCKED,
                "acquire refused: active lease held by "
                f"{held.holder_node_id!r} at term {held.term} "
                f"(state={held.state}), not expired — a second valid holder "
                "is forbidden (V1 mutual exclusion). Wait for release "
                "(FREE) or lease expiration",
                {
                    "active_holder": held.holder_node_id,
                    "active_term": held.term,
                    "active_state": held.state,
                },
            )

        # --- Effets (toutes les gardes ont passé) ---
        # Ordre IMPÉRATIF (never-drop/never-orphan — Codex BLOCKING head
        # 62e71dbc) : la consommation IRRÉVERSIBLE de la head (mark_granted) est
        # le DERNIER effet. Les trois écritures durables ne sont PAS
        # transactionnelles ; une panne entre elles ne doit JAMAIS consommer la
        # requête sans établir de holder (orphelin). En ordonnant bump_term ->
        # set_token -> mark_granted, toute panne AVANT mark_granted laisse la head
        # ENCORE PENDING (retryable). Une panne entre set_token et mark_granted
        # laisse un token HELD durable pour CE event_id : le fast-path de reprise
        # en tête finalise mark_granted au retry (idempotent), sans re-bumper le
        # term.
        #
        # 1. Bump du term DEPUIS le store (jamais un compteur externe), monotone.
        term_state = await self._store.get_term()
        new_term = (term_state.term if term_state is not None else 0) + 1
        await self._store.bump_term(new_term, updated_by_node_id=holder_node_id)
        # 2. Token HELD (fencing_token == new_term, invariant modèle) + lease : le
        # POINT DE COMMIT du grant. La dual-monotonie de set_token rejette toute
        # régression term/fencing (un write concurrent stale ne peut pas écraser).
        token = TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=holder_node_id,
            term=new_term,
            fencing_token=new_term,
            granted_at=now.isoformat(),
            lease_until=compute_lease_until(now, self._ttl_seconds),
            membership_epoch=(membership.epoch if membership is not None else 0),
            event_id=event_id,
            request_id=request_id,
        )
        granted_token = await self._store.set_token(token)
        # 3. Consume-once de la head EN DERNIER (effet IRRÉVERSIBLE) : la requête
        # ne sort de l'éligibilité qu'une fois le holder durablement établi.
        await self._queue.mark_granted(head)
        return granted_token

    # ─────────────────────────────────────────────────────────────────
    # renew — étendre la lease du holder courant SANS bumper le term
    # ─────────────────────────────────────────────────────────────────

    async def renew(self, *, holder_node_id: str) -> TokenLeaseState:
        """
        Prolonge ``lease_until`` du holder COURANT sans toucher au term/fencing.

        Gardes (lèvent, aucun write en cas d'échec) :
        - token HELD ET ``holder_node_id == holder`` -> sinon ``NOT_HOLDER`` ;
        - term du token == ``term.json`` vivant -> sinon ``STALE_TERM`` (Codex
          BLOCKING head 5225303) : un holder SUPERSEDED (``token.term < term.term``,
          un grant plus récent a bumpé le term) ne doit PAS prolonger sa lease
          obsolète. Sans ce garde, sa lease resterait « vivante » et l'exclusion
          mutuelle G3 d'``acquire`` la verrait active indéfiniment -> blocage de la
          convergence jusqu'à reconcile explicite (HIVEMIND.md §6.2 : un ancien
          holder revenu après un bump de term est fencé). On ré-assert contre le
          term VIVANT, on ne le ré-dérive jamais (même posture que la chaîne
          d'égalité STALE_TERM d'``evaluate_commit_authorization``) ;
        - lease NON expirée à ``now`` -> sinon ``FENCED`` (une lease déjà expirée
          ne peut pas être renouvelée en place ; il faut ré-acquérir via la
          queue, sinon le renew ressusciterait une lease que l'exclusion mutuelle
          a déjà cessé de protéger).

        Ordre des refus aligné sur ``evaluate_commit_authorization`` : corruption
        -> NOT_HOLDER -> STALE_TERM -> FENCED. Ré-écrit le MÊME token
        (term/fencing/holder/granted_at inchangés) avec un ``lease_until`` frais.
        ``set_token`` ré-écrit byte-stable.

        Fail-closed AVANT la dénégation holder (Codex MINOR head 62e71dbc) : un
        token ACTIF (HELD/RELEASING) corrompu (``lease_until`` None/malformé OU
        ``holder_node_id`` manquant) tenu par un AUTRE nœud doit remonter en
        ``CorruptedStateError``, JAMAIS être masqué en ``NOT_HOLDER`` — même
        posture structural-first que ``evaluate_commit_authorization`` /
        ``release`` / ``reconcile_stale_holder``. No-op sur un FREE, un actif sain,
        ou un token absent.

        Sous le verrou de mutation par-space (``_token_lock``, Codex HIGH head
        fb6f112) : le read-modify-write de renew est sérialisé avec acquire /
        release / reconcile, sinon un acquire concurrent verrait un snapshot
        périmé pendant le renew (split-brain).
        """
        async with _token_lock(self._space_id):
            await self._assert_mesh_mutation_allowed()
            return await self._renew_locked(holder_node_id=holder_node_id)

    async def _renew_locked(self, *, holder_node_id: str) -> TokenLeaseState:
        """Corps de ``renew`` exécuté SOUS le verrou de mutation par-space."""
        now = self._clock()
        current = await self._store.get_token()
        term_state = await self._store.get_term()
        # Corruption-first : un actif corrompu (même tenu par un autre) lève
        # CorruptedStateError avant tout refus « normal ». Deux dimensions :
        # structure de lease (lease_until/holder) ET cohérence term (term.json
        # absent ou token au futur). No-op si FREE / None.
        if current is not None:
            assert_active_lease_structural(current, now)
            assert_active_token_term_consistent(current, term_state)
        if (
            current is None
            or current.state != TokenState.HELD.value
            or current.holder_node_id != holder_node_id
        ):
            raise CommitNotAuthorized(
                CommitDenyReason.NOT_HOLDER,
                f"renew refused: {holder_node_id!r} does not hold the token "
                f"(state={current.state if current else None}, "
                f"holder={current.holder_node_id if current else None!r})",
                {
                    "token_state": current.state if current else None,
                    "token_holder": current.holder_node_id if current else None,
                    "asserting_holder": holder_node_id,
                },
            )
        # STALE_TERM : le holder doit être au term VIVANT. Un holder superseded
        # (token.term < term.json) ne peut pas prolonger sa lease obsolète — sinon
        # G3 la verrait active et bloquerait la convergence (Codex BLOCKING 5225303).
        # La garde de cohérence term ci-dessus a déjà fait remonter term.json absent
        # et token.term > term.json (corruption) ; ici term_state est non None et
        # current.term <= term_state.term, donc seul le cas superseded (<) reste.
        if current.term != term_state.term:
            raise CommitNotAuthorized(
                CommitDenyReason.STALE_TERM,
                f"renew refused: holder superseded (token.term={current.term} != "
                f"term.json={term_state.term}) — an old holder returning after a "
                "term bump is fenced, never re-authorized to extend its lease "
                "(HIVEMIND.md §6.2)",
                {"token_term": current.term, "current_term": term_state.term},
            )
        if is_lease_expired(current, now):
            raise CommitNotAuthorized(
                CommitDenyReason.FENCED,
                "renew refused: lease already expired "
                f"(lease_until={current.lease_until!r}, now={now.isoformat()}) — "
                "re-acquisition through the queue is required; no in-place renew",
                {"lease_until": current.lease_until, "now": now.isoformat()},
            )
        renewed = current.model_copy(
            update={"lease_until": compute_lease_until(now, self._ttl_seconds)}
        )
        return await self._store.set_token(renewed)

    # ─────────────────────────────────────────────────────────────────
    # release — libération volontaire (FREE, term/fencing préservés)
    # ─────────────────────────────────────────────────────────────────

    async def release(self, *, holder_node_id: str) -> TokenLeaseState:
        """
        Le holder libère volontairement le token.

        Garde : si le token est HELD/RELEASING, son holder doit être
        ``holder_node_id`` -> sinon ``NOT_HOLDER``. (Libérer un token déjà FREE au
        même holder est un no-op byte-stable.)

        Écrit un token FREE en PRÉSERVANT term/fencing_token (la monotonie de
        ``set_token`` rejette de toute façon une descente), ``holder_node_id=None``,
        ``membership_epoch`` porté. ``TokenState.FREE`` ne déclenche PAS
        l'invariant modèle ``fencing_token == term`` (réservé à HELD/RELEASING),
        donc un FREE préservant l'ancien term/fencing se construit sans erreur.
        Idempotent : re-release d'un FREE au même term = no-op byte-stable.

        Fail-closed (ADR-0011) : un token ACTIF (HELD/RELEASING) au ``lease_until``
        corrompu/incomplet (``None`` ou malformé) ne peut PAS être « libéré » en
        FREE — ce serait réparer/effacer silencieusement un état critique
        corrompu. ``assert_active_lease_structural`` (même validité de lease que
        les gates acquire/assert) lève ``CorruptedStateError`` avant tout write.

        Sous le verrou de mutation par-space (``_token_lock``, Codex HIGH head
        fb6f112) : sérialisé avec acquire / renew / reconcile.
        """
        async with _token_lock(self._space_id):
            await self._assert_mesh_mutation_allowed()
            return await self._release_locked(holder_node_id=holder_node_id)

    async def _release_locked(self, *, holder_node_id: str) -> TokenLeaseState:
        """Corps de ``release`` exécuté SOUS le verrou de mutation par-space."""
        now = self._clock()
        current = await self._store.get_token()
        if current is None:
            raise CommitNotAuthorized(
                CommitDenyReason.NOT_HOLDER,
                f"release refused: no token to release for {holder_node_id!r}",
                {"asserting_holder": holder_node_id},
            )
        # Fail-closed AVANT toute décision/écriture : un actif corrompu remonte
        # en CorruptedStateError, jamais transformé en FREE (même garde que les
        # gates qui passent par is_lease_expired). Deux dimensions : structure de
        # lease ET cohérence term (term.json absent ou token au futur ne peuvent
        # PAS être effacés en FREE silencieusement). No-op si FREE.
        assert_active_lease_structural(current, now)
        assert_active_token_term_consistent(current, await self._store.get_term())
        if (
            current.state in (TokenState.HELD.value, TokenState.RELEASING.value)
            and current.holder_node_id != holder_node_id
        ):
            raise CommitNotAuthorized(
                CommitDenyReason.NOT_HOLDER,
                f"release refused: {holder_node_id!r} does not hold the token "
                f"(holder={current.holder_node_id!r}, state={current.state})",
                {
                    "token_holder": current.holder_node_id,
                    "token_state": current.state,
                    "asserting_holder": holder_node_id,
                },
            )
        freed = TokenLeaseState(
            state=TokenState.FREE,
            holder_node_id=None,
            term=current.term,
            fencing_token=current.fencing_token,
            membership_epoch=current.membership_epoch,
        )
        return await self._store.set_token(freed)

    # ─────────────────────────────────────────────────────────────────
    # reconcile_stale_holder — démotion d'un holder superseded (par TERM)
    # ─────────────────────────────────────────────────────────────────

    async def reconcile_stale_holder(self) -> TokenLeaseState | None:
        """
        Démote un holder HELD/RELEASING superseded vers FREE au term COURANT.

        Réconcilie par TERM (supersession), PAS par horloge : un holder
        HELD/RELEASING dont ``token.term < store.get_term().term`` (un grant plus
        récent existe) est sorti de l'état actif en écrivant un FREE au term
        courant (fencing/term MONTENT, jamais descendre -> ``set_token``-safe),
        ``holder_node_id=None``. Cela retire le holder stale de l'ensemble
        {HELD, RELEASING} (plus de split-holder silencieux que
        ``assert_at_most_one_valid_holder`` attraperait).

        No-op (retourne le token courant / ``None``) si le token est absent, déjà
        inactif, ou au term courant (holder légitime). Lié à UN store : pas
        d'argument ``node_id``. Un holder expiré-mais-non-superseded est géré par
        ``is_lease_expired`` aux gates acquire/assert, pas ici.

        Fail-closed (ADR-0011) : un token ACTIF (HELD/RELEASING) au ``lease_until``
        corrompu/incomplet (``None`` ou malformé) n'est NI un holder courant
        légitime à préserver, NI un stale à effacer silencieusement en FREE — il
        remonte en ``CorruptedStateError`` (``assert_active_lease_structural``,
        même validité de lease que les gates) avant toute décision de
        garde/démotion. La réconciliation ne répare jamais une corruption
        critique.

        Sous le verrou de mutation par-space (``_token_lock``, Codex HIGH head
        fb6f112) : sérialisé avec acquire / renew / release.
        """
        async with _token_lock(self._space_id):
            await self._assert_mesh_mutation_allowed()
            return await self._reconcile_stale_holder_locked()

    async def _reconcile_stale_holder_locked(self) -> TokenLeaseState | None:
        """Corps de ``reconcile_stale_holder`` SOUS le verrou de mutation par-space."""
        now = self._clock()
        token = await self._store.get_token()
        if token is None:
            return None
        if token.state not in (
            TokenState.HELD.value,
            TokenState.RELEASING.value,
        ):
            return token  # déjà inactif : rien à réconcilier
        # Fail-closed AVANT toute décision (keep/démote) : un actif corrompu
        # remonte en CorruptedStateError, jamais préservé comme « légitime » ni
        # effacé silencieusement en FREE. Deux dimensions : structure de lease ET
        # cohérence term — un ``term.json`` absent ou un token AU FUTUR
        # (``token.term > term.json``) est impossible/corrompu et n'est NI un holder
        # courant à préserver NI un superseded à démoter (Codex BLOCKING 20e2e5b).
        assert_active_lease_structural(token, now)
        term_state = await self._store.get_term()
        assert_active_token_term_consistent(token, term_state)
        # La garde ci-dessus a fait remonter term.json absent et token.term >
        # term.json ; ici term_state est non None et token.term <= term_state.term.
        # token.term == term.json -> holder courant LÉGITIME (on garde) ;
        # token.term < term.json -> holder SUPERSEDED (on démote en FREE).
        if token.term == term_state.term:
            return token  # holder courant légitime au term courant : on garde
        reconciled = TokenLeaseState(
            state=TokenState.FREE,
            holder_node_id=None,
            term=term_state.term,
            fencing_token=term_state.term,
            membership_epoch=token.membership_epoch,
        )
        return await self._store.set_token(reconciled)

    # ─────────────────────────────────────────────────────────────────
    # assert_commit_allowed — LE point d'autorisation unique (ADR-0011)
    # ─────────────────────────────────────────────────────────────────

    async def assert_commit_allowed(self, intent: CommitIntent) -> None:
        """
        Point d'autorisation UNIQUE ADR-0011. Retourne ``None`` en cas de succès ;
        lève ``CommitNotAuthorized`` (raison stable) à la moindre défaillance.

        Prédicat pur d'état protocole : AUCUN write, AUCUN graph/long, AUCUN
        contrôle de membership/permission (ceux-ci tournent en amont). Charge
        ``token`` / ``term`` / ``pointer`` puis délègue à
        ``evaluate_commit_authorization`` avec l'instant ``now`` de l'horloge
        injectée.

        ``CorruptedStateError`` PROPAGE (fail-closed) : un ``token.json`` /
        ``term.json`` / ``bank_version.json`` corrompu BLOQUE l'autorisation, il
        n'est jamais converti en code de raison ni en default-allow.

        Symétrie pair (ADR-0011 §8) : le pair reconstruit un ``CommitIntent`` à
        partir du ``BANK_COMMIT`` reçu, lit SON propre ``token/term/pointer`` et
        appelle ``evaluate_commit_authorization(..., now=son_horloge())`` — la
        MÊME fonction pure. Un commit auto-autorisé chez l'émetteur à son ancien
        term est rejeté ``STALE_TERM`` car le term vivant du pair est supérieur.
        """
        # Snapshot LINÉARISABLE avec les mutations de token (Codex BLOCKING heads
        # a26cd28 + 4dc7855). ``assert_commit_allowed`` lit token/term/pointer EN
        # SÉQUENCE ; sans verrou, un ``release()`` concurrent (qui écrit FREE en
        # PRÉSERVANT term/fencing) peut s'intercaler ENTRE la lecture du token (HELD)
        # et celle du pointeur -> le prédicat autoriserait sur un snapshot HELD DÉJÀ
        # INVALIDÉ (token devenu FREE), permettant un BANK_COMMIT APRÈS release. On
        # lit donc le snapshot SOUS le verrou de mutation par-space : aucun
        # acquire/renew/release/reconcile ne peut écrire pendant la lecture, donc
        # token/term/pointer forment un état cohérent à un même instant.
        #
        # ``now`` est lu DANS le verrou, APRÈS les 3 reads (au point de linéarisation
        # du snapshot) : si l'appelant a ATTENDU le verrou (ou des reads lents) et a
        # FRANCHI ``lease_until`` pendant l'attente, l'expiry (``is_lease_expired``)
        # doit être évaluée à l'instant du snapshot, pas à un ``now`` pré-attente
        # périmé qui autoriserait une lease déjà expirée. AINSI les 4 inputs
        # store-dérivés de evaluate (token/term/pointer/now) viennent du MÊME instant
        # verrouillé. Lecture SEULE ; l'évaluation pure tourne HORS verrou sur le
        # snapshot (pas de réentrance, durée de verrou minimale). Le « check-then-act »
        # plus large autorisation->apply durable du BANK_COMMIT est porté par P5-6.
        async with _token_lock(self._space_id):
            token = await self._store.get_token()
            term = await self._store.get_term()
            pointer = await self._store.get_bank_version_pointer()
            now = self._clock()
        evaluate_commit_authorization(
            token=token,
            term=term,
            pointer=pointer,
            intent=intent,
            now=now,
        )
