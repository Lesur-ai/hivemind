# -*- coding: utf-8 -*-
"""
Runtime de réplication des short-notes Hivemind (issue #15 / P5-7). Mirror de
``commit_runtime`` : deux couches pure/async.

Ce module porte la **réplication asynchrone par-origine** des live-notes courtes
vers les pairs ACTIFS (HIVEMIND.md §5.2). Il NE PASSE PAS par le token ni par le
gate d'autorisation de commit (une note n'est pas un commit de bank partagé)
mais il RESPECTE strictement les tombstones et les watermarks.

ADRs portés :

- **ADR-0013** : identité de note par ``note_id`` (le stem du filename ; PAS de
  second ``origin_note_id`` physique — ``origin_note_id`` n'en est que l'alias
  accessor). DISTINCTION des deux watermarks : ce module n'avance QUE le curseur
  de POSITION d'event (``last_event_id``/``last_event_ts``) et PORTE
  ``bank_version`` (progrès appliqué) inchangé. Il ne lit/écrit JAMAIS
  ``bank_version`` comme curseur de replication, et le curseur d'event ne gate
  JAMAIS la GC des tombstones.
- **ADR-0012** : graph/long-memory local-only — ce module n'importe ni
  ``graph_push`` ni ``consolidation_queue``/``consolidator``/``long`` (vérifié
  par scan AST).

Invariants protocole portés :

- **anti-résurrection** : une note dont le ``note_id`` est tombstoné est REJETÉE
  à la réplication (``get_tombstone`` consulté EN PREMIER, gagne sous réordre) —
  jamais (re)créée sur un pair. Le tombstone qui arrive AVANT ou APRÈS la note
  gagne toujours ;
- **idempotence** : la copie durable ``live/{filename}`` est first-write-wins par
  clé (``note_id``) — re-livrer le même ``event_id`` est un no-op ;
- pas de mémoire longue/graph ; pas de timer (horloge injectée) ; pas de chemin
  token/commit ;
- **fail-closed** : aucun ``try/except`` autour des lectures de store — une
  ``CorruptedStateError`` propage (jamais lue comme « absent »).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from enum import Enum

from pydantic import Field

from ..live_note_format import (
    decode_live_note_string,
    split_live_note_front_matter,
)
from ..storage import StorageService
from . import layout
from .models import (
    Watermark,
    _HivemindBase,
    _now_iso,
)
from .layout import PROTOCOL_VERSION
from .state import HivemindStateStore


# =============================================================================
# Horloge injectable (même seam que commit_runtime / lease_runtime)
# =============================================================================


def _now_utc() -> datetime:
    """Instant courant UTC (aware). Seule lecture d'horloge murale du module,
    injectable via le seam ``clock=`` de ``NoteReplicationRuntime``."""
    return datetime.now(timezone.utc)


Clock = Callable[[], datetime]


# =============================================================================
# Couche pure — identité de note (le note_id == stem du filename)
# =============================================================================


def note_id_from_filename(filename: str) -> str:
    """
    ``"{stem}.md"`` -> ``"{stem}"``. Strippe l'UNIQUE suffixe ``.md`` final, qui
    est OBLIGATOIRE.

    Le ``note_id`` est l'identité unique d'une note (ADR-0013) — le MÊME stem que
    la clé du tombstone et du sidecar.

    FAIL-CLOSED sur l'extension : un filename qui ne finit PAS par ``.md`` est
    REJETÉ (``ValueError``). Sans cette garde, ``"foo"`` produirait ``note_id
    "foo"`` stocké sous ``live/foo``, que ``reap_on_tombstone`` (qui ne résout que
    ``{note_id}.md``) et l'oracle anti-résurrection (qui ignorait les objets
    non-``.md``) laisseraient SURVIVRE à son tombstone — une résurrection
    silencieuse. On refuse aussi un résultat vide ou contenant ``/`` (il évaderait
    le préfixe ``live/`` et casserait l'identité ; garde alignée sur
    ``layout.tombstone_key``).
    """
    if not filename.endswith(".md"):
        raise ValueError(
            f"filename de note invalide (doit finir par '.md'): {filename!r}"
        )
    stem = filename[:-3]
    if not stem or "/" in stem:
        raise ValueError(f"Invalid filename for a note_id: {filename!r}")
    return stem


def note_id_from_key(s3_key: str) -> str:
    """
    ``"{space}/live/{stem}.md"`` -> ``note_id_from_filename(basename)``.

    ``ValueError`` si la forme est mauvaise (pas de ``/``, ou stem contenant un
    ``/`` après strip de ``.md``).
    """
    if "/" not in s3_key:
        raise ValueError(f"Invalid S3 key (no '/'): {s3_key!r}")
    basename = s3_key.rsplit("/", 1)[-1]
    return note_id_from_filename(basename)


# =============================================================================
# Couche pure — statuts + modèles de payload / sidecar
# =============================================================================


class NoteReplicationConflictError(ValueError):
    """
    Levée quand une note dont le ``live/{filename}`` existe DÉJÀ est ré-livrée avec
    des octets durables (``note_md``) DIVERGENTS : même ``note_id`` (même identité
    logique — ADR-0013) mais contenu différent.

    Même doctrine que ``QueueReplayConflictError`` : un identifiant identifie UNE
    entité logique unique. Un rejeu FIDÈLE (octets byte-identiques) est idempotent
    (``DUPLICATE``, aucune seconde écriture) ; un rejeu DIVERGENT est fail-closed —
    on ne réécrit JAMAIS la copie durable existante et on ne le coalesce JAMAIS en
    succès silencieux.
    """


class ReplicationStatus(str, Enum):
    """
    Verdicts stables d'une tentative de réplication inbound. ``str``-Enum aligné
    sur la convention du codebase (``TokenState`` / ``CommitApplyReason``).
    """

    STORED = "stored"  # accepté + live/{filename} écrit + sidecar écrit
    DUPLICATE = "duplicate"  # live/{filename} déjà présent — aucune réécriture
    REJECTED_TOMBSTONED = "rejected_tombstoned"  # note_id tombstoné — jamais (re)créé
    REJECTED_IDENTITY = "rejected_identity"  # note_id != note_id_from_filename(filename)


class ReplicatedNote(_HivemindBase):
    """
    Le payload de réplication d'une short-note (source du wire + du sidecar).

    ``note_id`` == stem du ``filename`` (ADR-0013 : identité unique ;
    ``origin_note_id`` n'en est que l'alias accessor, JAMAIS un second champ
    physique). ``note_md`` porte les octets EXACTS du ``.md`` (front-matter +
    body) à réécrire verbatim côté pair — byte-identiques à l'origine, ce qui
    préserve le checksum du snapshot de bootstrap. ``content``/``category``/etc.
    sont des champs de commodité parsés (pour le sidecar / le label), JAMAIS
    re-sérialisés dans le ``.md``.
    """

    protocol_version: int = PROTOCOL_VERSION
    note_id: str
    filename: str  # "{stem}.md" — l'objet durable live/
    origin_node_id: str  # origine cryptographique = vérité d'affichage (signataire)
    origin_agent: str  # agent du front-matter (nom d'affichage)
    category: str = ""
    content: str = ""  # corps de la note (commodité ; jamais re-sérialisé)
    tags: list[str] = Field(default_factory=list)
    created_at: str = ""  # timestamp d'origine de la note (provenance préservée)
    note_md: str  # octets EXACTS du .md (front-matter + body) à écrire verbatim


class NoteOrigin(_HivemindBase):
    """
    Sidecar de provenance posé à ``live/_origin/{note_id}.json`` (P5-7).

    Comme les octets du ``.md`` sont préservés VERBATIM (pour conserver le checksum
    du snapshot de bootstrap), ce sidecar est le SEUL endroit durable de la
    provenance par-note. Il porte donc le tuple mandaté par HIVEMIND.md §2 point 7
    (« Provenance live-note durable ») : ``origin_node_id``, ``origin_agent``,
    ``origin_note_id`` (== ``note_id``, ADR-0013) ET ``event_id`` (la position
    d'event qui a livré la note). ``event_id`` est REQUIS (fail-closed) : sans lui
    la provenance d'event durable serait perdue.
    """

    protocol_version: int = PROTOCOL_VERSION
    note_id: str
    origin_node_id: str
    origin_agent: str
    event_id: str  # position d'event durable (HIVEMIND.md §2 point 7) — REQUIS
    created_at: str = ""
    replicated_at: str = Field(default_factory=_now_iso)


def _origin_of(
    note: ReplicatedNote, *, event_id: str, replicated_at: str
) -> NoteOrigin:
    """
    Construit le sidecar de provenance d'une note répliquée. Point UNIQUE de
    fabrication du ``NoteOrigin`` côté receiver (STORED et réparation du chemin
    DUPLICATE), de sorte que ``event_id`` — la provenance d'event durable mandatée
    par HIVEMIND.md §2 point 7 — soit TOUJOURS écrit, jamais oublié sur un chemin.
    """
    return NoteOrigin(
        note_id=note.note_id,
        origin_node_id=note.origin_node_id,
        origin_agent=note.origin_agent,
        event_id=event_id,
        created_at=note.created_at,
        replicated_at=replicated_at,
    )


class ReplicationResult(_HivemindBase):
    """Résultat d'un ``replicate_inbound`` : verdict + effets observables."""

    note_id: str
    status: ReplicationStatus
    persisted: bool  # True ssi un nouvel objet live/ a été écrit
    cursor_advanced: bool  # True ssi le curseur d'event a avancé


def origin_note_id(note: ReplicatedNote) -> str:
    """Accessor de l'ALIAS documenté ``origin_note_id`` -> retourne
    ``note.note_id``. JAMAIS un champ stocké distinct (ADR-0013 : pas de seconde
    clé physique)."""
    return note.note_id


def provenance_label(*, origin_agent: str, is_local: bool, peer_alias: str) -> str:
    """
    Libellé d'affichage de provenance (HIVEMIND.md §5.2) :

    - note d'origine locale -> ``"{origin_agent} @ local"`` ;
    - note d'origine pair   -> ``"{origin_agent} @ {peer_alias}"``.
    """
    where = "local" if is_local else peer_alias
    return f"{origin_agent} @ {where}"


# =============================================================================
# Couche pure — le curseur de POSITION d'event (JAMAIS bank_version, ADR-0013)
# =============================================================================


def advance_event_cursor(
    prev: Watermark | None,
    *,
    node_id: str,
    event_id: str,
    event_ts: str,
    term: int = 0,
    membership_epoch: int = 0,
) -> Watermark:
    """
    Avance le curseur de POSITION d'event (``last_event_id`` / ``last_event_ts``)
    pour ``node_id`` vers ``(event_id, event_ts)`` SSI ``prev`` est ``None`` OU
    ``event_ts > prev.last_event_ts`` (un ts plus ancien/égal NE rewind JAMAIS la
    position — voir ci-dessous).

    MONOTONICITÉ STRICTE DU CURSEUR (jamais de rollback — invariant protocole) :

    - ``last_event_id`` / ``last_event_ts`` n'avancent que si la position entrante
      est STRICTEMENT plus récente (``event_ts > prev.last_event_ts``). Un apply
      réordonné/plus ancien (ou égal) ne peut PAS rembobiner la position : on
      conserve la position de ``prev``. C'est ce qui protège un commit préparé
      « stale » qui s'écrirait APRÈS un curseur plus récent (cf.
      ``_advance_cursor`` qui relit/recalcule au moment d'écrire).
    - ``term`` / ``membership_epoch`` sont PORTÉS EN AVANT par ``max`` :
      ``max(prev.term, term)`` et ``max(prev.membership_epoch, membership_epoch)``.
      Le curseur ne peut JAMAIS faire décroître le ``term`` ou l'``epoch`` écrits
      par le commit_runtime (qui les pose avec les vraies valeurs ;
      ``advance_event_cursor`` les reçoit à ``0`` par défaut côté réplication —
      sans le ``max``, un note répliqué les écraserait à ``0``).
    - ``bank_version`` (progrès APPLIQUÉ, écrit par le commit_runtime, JAMAIS par
      la réplication) est PORTÉ inchangé (``prev.bank_version`` si ``prev`` sinon
      ``-1``). Sans ce report, ``set_watermark`` (garde monotone) lèverait dès
      qu'un commit a avancé ``bank_version`` à >= 0 (``-1 < 0``).

    Retourne ``prev`` INCHANGÉ (``is prev``) — donc no-op de write côté appelant —
    quand RIEN ne progresse : ni la position (entrante pas strictement plus
    récente), ni le ``term``, ni l'``epoch``. C'est le SEUL endroit où le curseur
    d'event avance.
    """
    prev_term = prev.term if prev is not None else 0
    prev_epoch = prev.membership_epoch if prev is not None else 0
    new_term = max(prev_term, term)
    new_epoch = max(prev_epoch, membership_epoch)

    position_advances = prev is None or event_ts > prev.last_event_ts

    # Rien à écrire : position pas strictement plus récente ET aucun report de
    # term/epoch à effectuer -> on rend ``prev`` tel quel (no-op de write).
    if (
        not position_advances
        and new_term == prev_term
        and new_epoch == prev_epoch
    ):
        return prev  # type: ignore[return-value]  # prev n'est None que si position_advances

    if position_advances:
        last_event_id = event_id
        last_event_ts = event_ts
    else:
        # Position pas strictement plus récente : on NE rewind PAS la position,
        # on ne fait que reporter term/epoch en avant.
        last_event_id = prev.last_event_id  # type: ignore[union-attr]
        last_event_ts = prev.last_event_ts  # type: ignore[union-attr]

    return Watermark(
        node_id=node_id,
        last_event_id=last_event_id,
        last_event_ts=last_event_ts,
        # PORT du progrès appliqué — jamais réécrit par la réplication (ADR-0013).
        bank_version=prev.bank_version if prev is not None else -1,
        # PORT MONOTONE de term/epoch — jamais décroissants (max(existing, in)).
        term=new_term,
        membership_epoch=new_epoch,
        event_id=last_event_id,
    )


def cursor_admits(prev: Watermark | None, *, event_ts: str) -> bool:
    """
    Prédicat pur de borne de replay : ``True`` si ``prev`` est ``None`` OU
    ``event_ts >= prev.last_event_ts``. Borne la reprise depuis le curseur
    d'event (on rejoue à partir de la position connue, inclusivement).
    """
    return prev is None or event_ts >= prev.last_event_ts


# =============================================================================
# Couche async — NoteReplicationRuntime (store-facing)
# =============================================================================


class NoteReplicationRuntime:
    """
    Wrapper async mince autour de ``HivemindStateStore`` + ``StorageService``
    pour la réplication idempotente des short-notes + l'anti-résurrection + le
    curseur d'event. Mirror structurel de ``CommitRuntime``.

    Il NE possède PAS la boucle de transport (signature/fan-out via
    ``HivemindPeerChannel`` est pilotée par le caller/harness) : il possède
    l'APPLY durable (``replicate_inbound``) et le seam de reap post-tombstone.

    Fail-closed : aucune méthode ne rattrape ``CorruptedStateError``. L'horloge
    est lue UNE FOIS par méthode publique (``now = self._clock()``).
    """

    def __init__(
        self,
        store: HivemindStateStore,
        storage: StorageService,
        space_id: str,
        *,
        clock: Clock = _now_utc,
    ) -> None:
        if space_id != store.space_id:
            raise ValueError(
                f"NoteReplicationRuntime space_id={space_id!r} != "
                f"store.space_id={store.space_id!r}"
            )
        self._store = store
        self._storage = storage
        self._space_id = space_id
        self._clock = clock

    @property
    def space_id(self) -> str:
        return self._space_id

    # ─────────────────────────────────────────────────────────────────
    # WRITER side — construire le payload à fan-out (la note locale existe déjà)
    # ─────────────────────────────────────────────────────────────────

    async def build_replicated_note(
        self, *, filename: str, local_node_id: str
    ) -> ReplicatedNote:
        """
        Lit ``live/{filename}`` (read-only — la note locale existe déjà), parse
        son front-matter (même forme que ``live._parse_note``) et retourne le
        ``ReplicatedNote`` à fan-out.

        ``note_id`` = stem du filename ; ``note_md`` = les octets bruts VERBATIM ;
        ``origin_node_id`` = ``local_node_id``. ``FileNotFoundError`` (ici
        ``ValueError``) si la note n'existe pas : le caller ne fan-out jamais une
        note absente.

        ANTI-RÉSURRECTION côté writer (defense-in-depth, fail-closed) : cette
        méthode CONSULTE elle-même ``get_tombstone(note_id)`` EN PREMIER et lève
        ``ValueError`` si la note est tombstonée — on ne ré-émet JAMAIS une note
        tombstonée vers les pairs, même si le caller a omis le skip préalable. Le
        caller DEVRAIT tout de même skipper en amont (cf. ``replicate_inbound``
        côté receiver) pour éviter le coût d'une construction inutile, mais le
        runtime ne dépend plus de cette discipline. ``CorruptedStateError`` PROPAGE
        (jamais lue comme « absent »).
        """
        note_id = note_id_from_filename(filename)
        # Garde anti-résurrection writer (EN PREMIER) : refuse de fabriquer un
        # payload pour une note déjà tombstonée localement (fail-closed).
        if await self._store.get_tombstone(note_id) is not None:
            raise ValueError(
                f"note tombstonée, ré-émission interdite (anti-résurrection): "
                f"{note_id!r}"
            )
        key = self._live_key(filename)
        raw = await self._storage.get(key)
        if raw is None:
            raise ValueError(
                f"note absente, impossible de la répliquer: {key!r}"
            )
        agent, category, tags, created_at, body = _parse_front_matter(raw)
        return ReplicatedNote(
            note_id=note_id,
            filename=filename,
            origin_node_id=local_node_id,
            origin_agent=agent,
            category=category,
            content=body,
            tags=tags,
            created_at=created_at,
            note_md=raw,
        )

    # ─────────────────────────────────────────────────────────────────
    # RECEIVER side — copie idempotente + anti-résurrection + curseur
    # ─────────────────────────────────────────────────────────────────

    async def replicate_inbound(
        self, *, note: ReplicatedNote, event_id: str, event_ts: str
    ) -> ReplicationResult:
        """
        Applique une note répliquée entrante. Ordre (les gardes d'abord, fermées) :

        1. **G-identité** : ``note_id_from_filename(note.filename) != note.note_id``
           -> ``REJECTED_IDENTITY``, AUCUN write, curseur NON avancé.
        2. **G-tombstone (ANTI-RÉSURRECTION, EN PREMIER, gagne sous réordre)** :
           ``get_tombstone(note_id)`` non-``None`` -> ``REJECTED_TOMBSTONED``,
           AUCUN write live/. Le curseur AVANCE quand même (la position d'event
           progresse même si la note est refusée — pas de boucle de re-livraison).
           ``CorruptedStateError`` PROPAGE (fail-closed).
        3. **Copie idempotente first-write-wins** par clé ``live/{filename}`` :
           déjà présente -> ``DUPLICATE`` (aucune réécriture) ; sinon ``put`` du
           ``note_md`` VERBATIM + écriture du sidecar -> ``STORED``.
        4. **Curseur de POSITION d'event** pour ``note.origin_node_id`` :
           ``advance_event_cursor`` (porte ``bank_version`` inchangé — ADR-0013).
        """
        now = self._clock()

        # G-identité (inclut la garde d'extension ``.md`` fail-closed). Un
        # filename extensionless / d'extension étrangère est REJETÉ ici même :
        # ``note_id_from_filename`` lève ``ValueError`` (le stem qui en dériverait
        # serait stocké sous un objet live/ que le reaper et l'oracle
        # n'attraperaient pas — résurrection silencieuse). On le traduit en
        # ``REJECTED_IDENTITY`` (aucun write, curseur NON avancé) plutôt que de
        # laisser fuiter l'exception : c'est un rejet de protocole, pas une
        # corruption de state critique.
        try:
            derived = note_id_from_filename(note.filename)
        except ValueError:
            return ReplicationResult(
                note_id=note.note_id,
                status=ReplicationStatus.REJECTED_IDENTITY,
                persisted=False,
                cursor_advanced=False,
            )
        if derived != note.note_id:
            return ReplicationResult(
                note_id=note.note_id,
                status=ReplicationStatus.REJECTED_IDENTITY,
                persisted=False,
                cursor_advanced=False,
            )

        # G-tombstone (anti-résurrection — consulté EN PREMIER, fail-closed).
        tombstone = await self._store.get_tombstone(note.note_id)
        if tombstone is not None:
            cursor_advanced = await self._advance_cursor(
                note.origin_node_id, event_id=event_id, event_ts=event_ts
            )
            return ReplicationResult(
                note_id=note.note_id,
                status=ReplicationStatus.REJECTED_TOMBSTONED,
                persisted=False,
                cursor_advanced=cursor_advanced,
            )

        # G-watermark (FAIL-CLOSED AVANT tout write durable) : on LIT/VALIDE le
        # curseur de POSITION d'event AVANT d'écrire ``live/{filename}`` + le
        # sidecar. Si le watermark de l'origine est corrompu, ``get_watermark``
        # lève ``CorruptedStateError`` ICI — AUCUNE copie live/ n'a encore été
        # écrite, donc la note reste INVISIBLE (pas de résurrection d'un état
        # critique corrompu). Sans ce réordre, une copie live/ visible
        # survivrait à la corruption du watermark. La VALEUR calculée ici n'est PAS
        # réutilisée telle quelle au commit (cf. ``_advance_cursor`` qui RELIT et
        # RE-DÉRIVE) — c'est uniquement le seam de validation fail-closed.
        await self._prepare_cursor(
            note.origin_node_id, event_id=event_id, event_ts=event_ts
        )

        # Copie durable idempotente (first-write-wins par clé). À ce stade le
        # watermark a déjà été lu/validé sans corruption.
        key = self._live_key(note.filename)
        if await self._storage.exists(key):
            persisted = False
            status = ReplicationStatus.DUPLICATE
            # DUPLICATE n'est idempotent QUE si les octets durables sont
            # byte-identiques. Même note_id + octets DIVERGENTS = même identité
            # logique, contenu différent -> ERREUR PROTOCOLE fail-closed (jamais
            # coalescée en succès silencieux ; doctrine ``QueueReplayConflictError``).
            # On ne réécrit JAMAIS la copie existante.
            existing = await self._storage.get(key)
            if existing != note.note_md:
                raise NoteReplicationConflictError(
                    f"note_id {note.note_id!r} déjà répliqué sous {key!r} avec des "
                    f"octets durables divergents (même identité, contenu différent) "
                    f"— fail-closed, aucune réécriture"
                )
            # Octets identiques : RÉPARE un sidecar de provenance manquant. Une
            # tentative antérieure a pu crasher ENTRE ``put(live/)`` et
            # ``_put_origin`` (deux objets S3 distincts, pas de transaction),
            # laissant la copie SANS provenance — affichée à tort comme LOCALE.
            # ``read_origin`` est fail-closed (un sidecar corrompu propage) ;
            # ``event_id`` provient de CETTE livraison.
            if await self.read_origin(note.note_id) is None:
                await self._put_origin(
                    _origin_of(note, event_id=event_id, replicated_at=now.isoformat())
                )
        else:
            await self._storage.put(key, note.note_md)
            await self._put_origin(
                _origin_of(note, event_id=event_id, replicated_at=now.isoformat())
            )
            persisted = True
            status = ReplicationStatus.STORED

        # Commit du curseur (le write du watermark vient APRÈS la copie). On RELIT
        # le watermark courant et on RE-DÉRIVE via ``advance_event_cursor`` à
        # l'instant de l'écriture : c'est ce qui rend le commit IDEMPOTENT et
        # MONOTONE même si un apply concurrent/réordonné a déjà avancé le curseur
        # entre la préparation et le commit. Une position préparée « stale » ne
        # peut PAS rembobiner ``last_event_ts`` (re-lecture -> position pas
        # strictement plus récente -> no-op). Voir le résiduel S3 documenté plus
        # bas.
        cursor_advanced = await self._advance_cursor(
            note.origin_node_id, event_id=event_id, event_ts=event_ts
        )
        return ReplicationResult(
            note_id=note.note_id,
            status=status,
            persisted=persisted,
            cursor_advanced=cursor_advanced,
        )

    async def _prepare_cursor(
        self, origin_node_id: str, *, event_id: str, event_ts: str
    ) -> Watermark | None:
        """
        LIT le watermark de POSITION d'event de ``origin_node_id`` et calcule le
        prochain (jamais ``bank_version`` — ADR-0013) — SEAM DE VALIDATION
        FAIL-CLOSED. Retourne le watermark calculé, ou ``None`` si le curseur
        n'avancerait pas, mais cette valeur n'est PAS persistée telle quelle :
        ``_advance_cursor`` (appelé au moment d'écrire) RELIT et RE-DÉRIVE pour
        rester monotone sous concurrence/réordre. ``CorruptedStateError`` PROPAGE
        (fail-closed) — cette lecture de l'état critique se fait AVANT tout write
        durable de la note dans ``replicate_inbound``.
        """
        prev = await self._store.get_watermark(origin_node_id)
        nxt = advance_event_cursor(
            prev, node_id=origin_node_id, event_id=event_id, event_ts=event_ts
        )
        return None if nxt is prev else nxt

    async def _advance_cursor(
        self, origin_node_id: str, *, event_id: str, event_ts: str
    ) -> bool:
        """
        Avance le curseur de POSITION d'event de ``origin_node_id`` (jamais
        ``bank_version`` — ADR-0013) en LISANT + DÉRIVANT + ÉCRIVANT au moment
        même de l'écriture. Retourne ``True`` ssi un write a eu lieu.

        SEAM MONOTONE / IDEMPOTENT (read-derive-write à l'instant du commit) :
        utilisé par TOUS les chemins qui persistent le curseur — tombstone-first
        ET STORED. Sur le chemin STORED, ``_prepare_cursor`` a déjà lu/validé le
        watermark (fail-closed) AVANT la copie durable ; cette voie RELIT pourtant
        l'état COURANT et le re-dérive via ``advance_event_cursor``, de sorte
        qu'un apply concurrent/réordonné ayant avancé le curseur entre-temps ne
        soit JAMAIS rembobiné (la position préparée « stale » devient un no-op à
        la re-lecture). ``advance_event_cursor`` reporte aussi term/epoch en
        avant (``max``) — jamais de rollback.

        RÉSIDUEL S3 (pas de transaction multi-clé) : la copie ``live/{filename}``
        et le write du watermark restent deux objets distincts ; cette voie ne
        garantit donc pas l'atomicité globale copie+curseur, mais le curseur est
        un roll-forward idempotent et monotone (la re-lecture absorbe tout
        réordre), borné par la garde monotone de ``set_watermark``.
        """
        prev = await self._store.get_watermark(origin_node_id)
        nxt = advance_event_cursor(
            prev, node_id=origin_node_id, event_id=event_id, event_ts=event_ts
        )
        if nxt is prev:
            return False
        # set_watermark garde la monotonicité sur bank_version ; advance_event_cursor
        # a PORTÉ bank_version inchangé, donc bank_version == existing.bank_version
        # -> la garde passe (jamais '<').
        await self._store.set_watermark(nxt)
        return True

    # ─────────────────────────────────────────────────────────────────
    # Reap post-tombstone (réordre note-first) — seam nommé, pas un afterthought
    # ─────────────────────────────────────────────────────────────────

    async def reap_on_tombstone(self, note_id: str) -> bool:
        """
        Supprime la copie ``live/{filename}`` + le sidecar ``live/_origin/`` si
        présents (delete-of-absent = no-op). C'est le seam où le tombstone-apply
        (commit_runtime étape 3, ou un ``TOMBSTONE_RECORDED`` répliqué) rencontre
        l'anti-résurrection : SANS lui, une copie écrite AVANT le tombstone
        survivrait et l'oracle ``assert_no_tombstone_resurrection`` la flaggerait.

        Le filename est récupéré depuis le sidecar (``read_origin``) ; à défaut
        (pas de sidecar), on scanne ``live/`` pour un stem == ``note_id``.
        Retourne ``True`` ssi une copie live a été supprimée.
        """
        filename = await self._resolve_filename(note_id)
        removed = False
        if filename is not None:
            key = self._live_key(filename)
            if await self._storage.exists(key):
                await self._storage.delete(key)
                removed = True
        # Le sidecar part dans tous les cas (best-effort, idempotent).
        await self._storage.delete(layout.origin_key(self._space_id, note_id))
        return removed

    async def _resolve_filename(self, note_id: str) -> str | None:
        """Récupère le filename d'un ``note_id`` via le sidecar, sinon par scan
        de ``live/`` (stem == note_id). ``None`` si aucune copie live."""
        origin = await self.read_origin(note_id)
        if origin is not None:
            return f"{note_id}.md"
        # Scan de secours (pas de sidecar) : un objet live/{note_id}.md.
        candidate = f"{note_id}.md"
        if await self._storage.exists(self._live_key(candidate)):
            return candidate
        return None

    # ─────────────────────────────────────────────────────────────────
    # Provenance sidecar — lecture (fail-closed via _get_model du store)
    # ─────────────────────────────────────────────────────────────────

    async def read_origin(self, note_id: str) -> NoteOrigin | None:
        """
        Lit le sidecar de provenance ``live/_origin/{note_id}.json``. ``None`` si
        absent. ``CorruptedStateError`` PROPAGE (sidecar cassé/schéma invalide —
        on réutilise la désérialisation fail-closed de ``HivemindStateStore``).
        """
        return await self._store._get_model(  # type: ignore[return-value]
            layout.origin_key(self._space_id, note_id), NoteOrigin
        )

    async def _put_origin(self, origin: NoteOrigin) -> None:
        await self._store._put_model(
            layout.origin_key(self._space_id, origin.note_id), origin
        )

    # ─────────────────────────────────────────────────────────────────
    # Resume — borne le replay sur le curseur d'event (jamais bank_version)
    # ─────────────────────────────────────────────────────────────────

    async def resume_replication(
        self, *, origin_node_id: str, candidates: list[tuple[ReplicatedNote, str]]
    ) -> list[ReplicatedNote]:
        """
        Reprend la réplication depuis le curseur de POSITION d'event de
        ``origin_node_id``. ``candidates`` = ``[(note, event_ts), ...]``.

        Retourne les notes du même origine dont ``event_ts`` est ADMIS par le
        curseur (``cursor_admits``). NE lit/écrit JAMAIS ``bank_version`` —
        découplé de la GC (ADR-0013, distinction des deux watermarks).
        """
        wm = await self._store.get_watermark(origin_node_id)
        return [
            note
            for note, event_ts in candidates
            if note.origin_node_id == origin_node_id
            and cursor_admits(wm, event_ts=event_ts)
        ]

    # ─────────────────────────────────────────────────────────────────
    # Helpers de clé
    # ─────────────────────────────────────────────────────────────────

    def _live_key(self, filename: str) -> str:
        """Clé S3 d'une note live VIVANTE (``{space}/live/{filename}``)."""
        if not filename or "/" in filename:
            raise ValueError(f"Invalid filename: {filename!r}")
        return f"{self._space_id}/live/{filename}"


# =============================================================================
# Couche pure — parsing du front-matter (même forme que live._parse_note)
# =============================================================================


def _parse_front_matter(raw: str) -> tuple[str, str, list[str], str, str]:
    """
    Parse le front-matter YAML simple d'une note ``.md`` avec le même helper
    pur de délimitation que ``live._parse_note`` (sans cycle de modules).

    Retourne ``(agent, category, tags, created_at, body)``. Tolérant : un
    front-matter absent/malformé retourne des champs vides + le body brut.
    """
    import json as _json

    parsed = split_live_note_front_matter(raw)
    if parsed is None:
        front_matter_str = ""
        body = raw.strip()
    else:
        front_matter_str, body = parsed

    fm: dict[str, str] = {}
    for line in front_matter_str.split("\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = decode_live_note_string(v)

    tags: list[str] = []
    tags_raw = fm.get("tags", "")
    if tags_raw.startswith("["):
        try:
            tags = _json.loads(tags_raw)
        except _json.JSONDecodeError:
            tags = []

    return (
        fm.get("agent", ""),
        fm.get("category", ""),
        tags,
        fm.get("timestamp", ""),
        body,
    )
