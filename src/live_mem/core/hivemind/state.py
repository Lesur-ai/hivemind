# -*- coding: utf-8 -*-
"""
Persistence de l'état protocole Hivemind (issue #3).

Conception :

- L'event journal ``events/`` est la source de vérité pour la
  **déduplication** : appliquer le même ``event_id`` deux fois est un
  no-op déterministe.
- Les fichiers d'état (``node.json``, ``members.json``, ``term.json``,
  ``token.json``, ``queue/``, ``acks/``, ``commits/``, ``tombstones/``,
  ``watermarks/``) sont des **vues dérivées** maintenues par les helpers
  ci-dessous. Une vue peut être reconstruite à partir des events si elle
  est corrompue (cf. ``rebuild_pointer_from_commits``).
- Toute désérialisation passe par les modèles Pydantic du module
  ``models``. Une JSONDecodeError ou ValidationError est convertie en
  ``CorruptedStateError`` — le store ne tente PAS de « réparer
  silencieusement » : il signale au caller, qui décide.

Idempotence des writes :

- ``append_event`` retourne ``False`` si l'``event_id`` est déjà connu
  (présence d'au moins un objet S3 sous ``events/*_{event_id}.json``).
- Les writes ciblés (token, term, membership, queue, acks, commits,
  tombstones, watermarks) sont stateless : passer deux fois le même
  payload produit le même fichier S3 — ré-écriture autorisée.
- ``record_ack(event_id, node_id, ...)`` ré-applique le même contenu si
  rejoué : la clé est ``{event_id}/{node_id}.json``.

Rétention et compaction (cf. DESIGN/live-mem/HIVEMIND_STATE.md) :

- ``events/`` est append-only ; ``compact_events_before`` permet de
  rejeter une fenêtre antérieure à un ``bank_version`` (= snapshot
  causal). Les opérateurs sont libres de l'appeler depuis un job
  d'entretien.
- ``commits/`` est conservé sans compaction par défaut (utile pour
  audit). Une politique LATER pourra n'en garder que les N derniers.
- ``tombstones/`` doit survivre jusqu'à ce que TOUS les peers aient un
  ``watermark`` dépassant le ``bank_version`` qui a consommé la note —
  ``garbage_collect_tombstones`` applique cette règle.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Optional

from pydantic import BaseModel, ValidationError

from ..storage import StorageService
from . import layout
from .models import (
    Ack,
    BankCommit,
    BankVersionPointer,
    CorruptedStateError,
    EventEnvelope,
    HiveNodeStatus,
    HivemindStateSnapshot,
    Member,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    QueueEntry,
    QueueEntryStatus,
    TermState,
    TokenLeaseState,
    Tombstone,
    Watermark,
)

logger = logging.getLogger("live_mem.hivemind.state")


# =============================================================================
# HivemindStateStore — point d'entrée unique
# =============================================================================


class HivemindStateStore:
    """
    Façade async pour lire/écrire l'état protocole d'un space sur S3.

    Le store ne tient AUCUN cache : chaque appel fait l'I/O. Les locks
    distribués (token Hivemind) et la concurrence intra-process
    (``asyncio.Lock``) sont gérés par les couches au-dessus (issues #6, #7).

    Args:
        storage: ``StorageService`` (S3) — injecté pour faciliter les tests.
        space_id: identifiant du space cible.
    """

    def __init__(self, storage: StorageService, space_id: str) -> None:
        if not space_id:
            raise ValueError("space_id requis")
        self._storage = storage
        self._space_id = space_id

    @property
    def space_id(self) -> str:
        return self._space_id

    # ─────────────────────────────────────────────────────────────────
    # Sérialisation / désérialisation Pydantic ↔ JSON
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _dump(model: BaseModel) -> dict[str, Any]:
        # mode="json" force les enums et datetimes vers une représentation
        # JSON-safe (str), évite un double passage à json.dumps avec
        # custom encoder côté S3.
        return model.model_dump(mode="json")

    async def _put_model(self, key: str, model: BaseModel) -> None:
        await self._storage.put_json(key, self._dump(model))

    async def _get_model(
        self, key: str, model_cls: type[BaseModel]
    ) -> Optional[BaseModel]:
        raw = await self._storage.get(key)
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CorruptedStateError(
                f"État Hivemind corrompu sur '{key}' : JSON invalide ({e})"
            ) from e
        try:
            return model_cls.model_validate(data)
        except ValidationError as e:
            raise CorruptedStateError(
                f"État Hivemind corrompu sur '{key}' : schéma {model_cls.__name__} invalide ({e})"
            ) from e

    # ─────────────────────────────────────────────────────────────────
    # Node identity
    # ─────────────────────────────────────────────────────────────────

    async def get_node_identity(self) -> Optional[NodeIdentity]:
        return await self._get_model(layout.node_key(self._space_id), NodeIdentity)  # type: ignore[return-value]

    async def set_node_identity(self, identity: NodeIdentity) -> NodeIdentity:
        """
        Écrit l'identité locale. Idempotent : écrire le même objet est OK.

        Si une identité existe déjà avec un ``node_id`` différent, on lève
        ``RuntimeError`` — changer le node_id silencieusement détruirait
        l'ancrage des ACKs distants et est presque toujours un bug.
        """
        existing = await self.get_node_identity()
        if existing and existing.node_id != identity.node_id:
            raise RuntimeError(
                f"NodeIdentity déjà initialisé avec node_id={existing.node_id!r}, "
                f"refus d'écrire un node_id différent {identity.node_id!r}"
            )
        await self._put_model(layout.node_key(self._space_id), identity)
        return identity

    # ─────────────────────────────────────────────────────────────────
    # Node health (node-local, critique — séparé de la membership partagée)
    # ─────────────────────────────────────────────────────────────────

    async def get_node_status(self) -> Optional[NodeHealth]:
        """
        Lit la santé node-local. ``None`` si le fichier est absent —
        le caller traite l'absence comme ``DISABLED`` (jamais ``HEALTHY``).

        Fichier critique : un JSON cassé ou un schéma invalide lève
        ``CorruptedStateError`` plutôt que d'être interprété comme sain.
        """
        return await self._get_model(  # type: ignore[return-value]
            layout.node_status_key(self._space_id), NodeHealth
        )

    async def set_node_status(self, health: NodeHealth) -> NodeHealth:
        """
        Écrit la santé node-local. Pas de garde de monotonicité : la santé
        peut régresser (HEALTHY → UNSAFE en début d'import, par exemple).

        Idempotent : ré-écrire le même contenu produit le même objet.
        """
        await self._put_model(layout.node_status_key(self._space_id), health)
        return health

    # ─────────────────────────────────────────────────────────────────
    # Membership view
    # ─────────────────────────────────────────────────────────────────

    async def get_membership(self) -> Optional[MembershipView]:
        return await self._get_model(layout.members_key(self._space_id), MembershipView)  # type: ignore[return-value]

    async def set_membership(self, view: MembershipView) -> MembershipView:
        """
        Remplace la membership view.

        Garde-fou : un nouvel ``epoch`` strictement inférieur au courant
        est rejeté — c'est le seul invariant local qu'on protège (les
        invariants distribués vivent dans le protocole, pas ici).
        """
        existing = await self.get_membership()
        if existing and view.epoch < existing.epoch:
            raise RuntimeError(
                f"Refus de descendre l'epoch membership : "
                f"courant={existing.epoch}, nouveau={view.epoch}"
            )
        await self._put_model(layout.members_key(self._space_id), view)
        return view

    # ─────────────────────────────────────────────────────────────────
    # Term
    # ─────────────────────────────────────────────────────────────────

    async def get_term(self) -> Optional[TermState]:
        return await self._get_model(layout.term_key(self._space_id), TermState)  # type: ignore[return-value]

    async def bump_term(self, new_term: int, updated_by_node_id: str) -> TermState:
        """
        Avance le term. Ne peut que monter (monotonicité).

        Idempotent : passer un ``new_term`` égal au courant ne fait rien
        et retourne l'état actuel.
        """
        existing = await self.get_term()
        current = existing.term if existing else 0
        if new_term < current:
            raise RuntimeError(
                f"Term monotone : refus de redescendre {current} → {new_term}"
            )
        if new_term == current and existing is not None:
            return existing
        next_state = TermState(term=new_term, updated_by_node_id=updated_by_node_id)
        await self._put_model(layout.term_key(self._space_id), next_state)
        return next_state

    # ─────────────────────────────────────────────────────────────────
    # Token lease
    # ─────────────────────────────────────────────────────────────────

    async def get_token(self) -> Optional[TokenLeaseState]:
        return await self._get_model(layout.token_key(self._space_id), TokenLeaseState)  # type: ignore[return-value]

    async def set_token(self, token: TokenLeaseState) -> TokenLeaseState:
        """
        Remplace l'état du token.

        Le séquencement (qui peut écrire quoi) est imposé par le protocole
        au-dessus (issue #7) ; ici on protège la monotonicité de deux
        invariants locaux :

        - ``term`` ne peut pas descendre (un grant d'un term antérieur ne
          peut pas écraser un état issu d'un term plus récent — sinon un
          replay rétrograde le fencing) ;
        - ``fencing_token`` ne peut pas descendre.

        L'invariant ``fencing_token == term`` pour les états actifs
        (``HELD``/``RELEASING``) est porté par le modèle ``TokenLeaseState``
        lui-même (validator Pydantic) — il est rejeté à l'instanciation
        avant même d'atteindre le store.
        """
        existing = await self.get_token()
        if existing:
            if token.term < existing.term:
                raise RuntimeError(
                    f"Term monotone sur le token : refus {existing.term} → {token.term}"
                )
            if token.fencing_token < existing.fencing_token:
                raise RuntimeError(
                    f"Fencing token monotone : refus {existing.fencing_token} → {token.fencing_token}"
                )
        await self._put_model(layout.token_key(self._space_id), token)
        return token

    # ─────────────────────────────────────────────────────────────────
    # Bank version pointer
    # ─────────────────────────────────────────────────────────────────

    async def get_bank_version_pointer(self) -> Optional[BankVersionPointer]:
        return await self._get_model(  # type: ignore[return-value]
            layout.bank_version_key(self._space_id), BankVersionPointer
        )

    async def set_bank_version_pointer(
        self, pointer: BankVersionPointer
    ) -> BankVersionPointer:
        existing = await self.get_bank_version_pointer()
        if existing and pointer.bank_version < existing.bank_version:
            raise RuntimeError(
                f"bank_version monotone : refus {existing.bank_version} → {pointer.bank_version}"
            )
        await self._put_model(layout.bank_version_key(self._space_id), pointer)
        return pointer

    # ─────────────────────────────────────────────────────────────────
    # Queue FIFO
    # ─────────────────────────────────────────────────────────────────

    async def enqueue(self, entry: QueueEntry) -> QueueEntry:
        """
        Ajoute une entrée en queue. Idempotent : ré-écrire la même entrée
        produit le même fichier S3 (même clé).

        Le caller est responsable du séquencement (allouer un ``sequence``
        monotone via le protocole). Le store ne le devine pas pour rester
        sans état partagé.
        """
        key = layout.queue_entry_key(self._space_id, entry.sequence, entry.event_id)
        await self._put_model(key, entry)
        return entry

    async def list_queue(self) -> list[QueueEntry]:
        """Liste les entrées en queue, ordonnées par ``sequence`` croissant."""
        objects = await self._storage.list_objects(layout.queue_prefix(self._space_id))
        entries: list[QueueEntry] = []
        for obj in sorted(objects, key=lambda o: o["Key"]):
            model = await self._get_model(obj["Key"], QueueEntry)
            if model is not None:
                entries.append(model)  # type: ignore[arg-type]
        return entries

    async def update_queue_entry_status(
        self, entry: QueueEntry, status: QueueEntryStatus
    ) -> QueueEntry:
        """
        Met à jour le ``status`` d'une entrée existante. Idempotent : ré-écrire
        le même status sur la même entrée est OK.
        """
        # Pydantic v2 : créer une copie modifiée sans muter l'original.
        next_entry = entry.model_copy(update={"status": status.value})
        key = layout.queue_entry_key(self._space_id, entry.sequence, entry.event_id)
        await self._put_model(key, next_entry)
        return next_entry

    async def remove_queue_entry(self, entry: QueueEntry) -> None:
        """Supprime une entrée de queue (ex: après grant)."""
        key = layout.queue_entry_key(self._space_id, entry.sequence, entry.event_id)
        await self._storage.delete(key)

    # ─────────────────────────────────────────────────────────────────
    # ACKs
    # ─────────────────────────────────────────────────────────────────

    async def record_ack(self, ack: Ack) -> Ack:
        """
        Persiste un ACK individuel. Idempotent : même (event_id, ack_by_node_id)
        écrit le même fichier ; les payload_hash divergents sont laissés au
        protocole (ne pas masquer un peer qui ACK des données stale).
        """
        key = layout.ack_key(self._space_id, ack.event_id, ack.ack_by_node_id)
        await self._put_model(key, ack)
        return ack

    async def list_acks(self, event_id: str) -> list[Ack]:
        prefix = layout.ack_prefix(self._space_id, event_id)
        objects = await self._storage.list_objects(prefix)
        acks: list[Ack] = []
        for obj in objects:
            model = await self._get_model(obj["Key"], Ack)
            if model is not None:
                acks.append(model)  # type: ignore[arg-type]
        return acks

    async def count_acks(self, event_id: str) -> int:
        """
        Nombre d'ACK valides pour un event.

        Délègue à ``list_acks`` pour appliquer la même politique de
        validation Pydantic : un objet corrompu ou de schéma invalide
        sous ``acks/{event_id}/`` ferait remonter ``CorruptedStateError``
        au caller au lieu d'être compté silencieusement dans un quorum.
        """
        return len(await self.list_acks(event_id))

    # ─────────────────────────────────────────────────────────────────
    # Bank commits
    # ─────────────────────────────────────────────────────────────────

    async def append_commit(self, commit: BankCommit) -> BankCommit:
        """
        Persiste un commit de bank.

        Idempotent : ré-appliquer le même ``commit_id`` sur la même
        ``bank_version`` est un no-op. Si la version existe DÉJÀ avec un
        ``commit_id`` différent, on lève — c'est une divergence protocole
        qui doit remonter.
        """
        key = layout.commit_key(self._space_id, commit.bank_version)
        existing = await self._get_model(key, BankCommit)
        if existing is not None:
            existing_commit: BankCommit = existing  # type: ignore[assignment]
            if existing_commit.commit_id == commit.commit_id:
                return existing_commit
            raise RuntimeError(
                f"Conflit de commit : bank_version={commit.bank_version} déjà "
                f"écrit avec commit_id={existing_commit.commit_id!r}, "
                f"nouveau commit_id={commit.commit_id!r}"
            )
        await self._put_model(key, commit)
        return commit

    async def get_commit(self, bank_version: int) -> Optional[BankCommit]:
        return await self._get_model(  # type: ignore[return-value]
            layout.commit_key(self._space_id, bank_version), BankCommit
        )

    async def list_commits(self, since_bank_version: int = -1) -> list[BankCommit]:
        """
        Liste les commits dans l'ordre croissant de ``bank_version``. Si
        ``since_bank_version`` est fourni, ne retourne que les commits
        strictement supérieurs.
        """
        objects = await self._storage.list_objects(layout.commit_prefix(self._space_id))
        commits: list[BankCommit] = []
        for obj in sorted(objects, key=lambda o: o["Key"]):
            model = await self._get_model(obj["Key"], BankCommit)
            if model is not None:
                commit: BankCommit = model  # type: ignore[assignment]
                if commit.bank_version > since_bank_version:
                    commits.append(commit)
        return commits

    async def latest_commit(self) -> Optional[BankCommit]:
        commits = await self.list_commits()
        return commits[-1] if commits else None

    async def rebuild_pointer_from_commits(self) -> Optional[BankVersionPointer]:
        """
        Reconstruit ``bank_version.json`` à partir du dernier commit présent
        dans ``commits/``. Utile en récupération après corruption du
        pointeur (cf. DESIGN/live-mem/HIVEMIND_STATE.md §rétention).
        """
        latest = await self.latest_commit()
        if latest is None:
            return None
        pointer = BankVersionPointer(
            bank_version=latest.bank_version,
            commit_id=latest.commit_id,
        )
        await self._put_model(layout.bank_version_key(self._space_id), pointer)
        return pointer

    # ─────────────────────────────────────────────────────────────────
    # Tombstones
    # ─────────────────────────────────────────────────────────────────

    async def add_tombstone(self, tombstone: Tombstone) -> Tombstone:
        key = layout.tombstone_key(self._space_id, tombstone.note_id)
        await self._put_model(key, tombstone)
        return tombstone

    async def get_tombstone(self, note_id: str) -> Optional[Tombstone]:
        return await self._get_model(  # type: ignore[return-value]
            layout.tombstone_key(self._space_id, note_id), Tombstone
        )

    async def list_tombstones(self) -> list[Tombstone]:
        objects = await self._storage.list_objects(
            layout.tombstone_prefix(self._space_id)
        )
        tombs: list[Tombstone] = []
        for obj in objects:
            model = await self._get_model(obj["Key"], Tombstone)
            if model is not None:
                tombs.append(model)  # type: ignore[arg-type]
        return tombs

    async def garbage_collect_tombstones(
        self, min_bank_version_across_watermarks: int
    ) -> int:
        """
        Supprime les tombstones consommés AVANT ``min_bank_version_across_watermarks``.

        Le caller doit calculer le min des ``bank_version`` de tous les
        peers ; cette fonction ne le fait pas pour rester découplée de la
        politique d'éviction d'un peer down (cf. issue #11).

        Returns:
            Nombre de tombstones supprimés.
        """
        deleted = 0
        for tomb in await self.list_tombstones():
            # bank_version == -1 = pas associé à un commit → on garde.
            if 0 <= tomb.bank_version < min_bank_version_across_watermarks:
                await self._storage.delete(
                    layout.tombstone_key(self._space_id, tomb.note_id)
                )
                deleted += 1
        return deleted

    # ─────────────────────────────────────────────────────────────────
    # Watermarks
    # ─────────────────────────────────────────────────────────────────

    async def set_watermark(self, watermark: Watermark) -> Watermark:
        """
        Met à jour la watermark d'un peer. Idempotent : ré-écriture du
        même objet OK. Garde-fou : ``bank_version`` ne peut pas
        descendre — la watermark représente le progrès.
        """
        existing = await self.get_watermark(watermark.node_id)
        if existing and watermark.bank_version < existing.bank_version:
            raise RuntimeError(
                f"Watermark monotone : refus {existing.bank_version} "
                f"→ {watermark.bank_version} pour node {watermark.node_id}"
            )
        key = layout.watermark_key(self._space_id, watermark.node_id)
        await self._put_model(key, watermark)
        return watermark

    async def get_watermark(self, node_id: str) -> Optional[Watermark]:
        return await self._get_model(  # type: ignore[return-value]
            layout.watermark_key(self._space_id, node_id), Watermark
        )

    async def list_watermarks(self) -> list[Watermark]:
        objects = await self._storage.list_objects(
            layout.watermark_prefix(self._space_id)
        )
        wms: list[Watermark] = []
        for obj in objects:
            model = await self._get_model(obj["Key"], Watermark)
            if model is not None:
                wms.append(model)  # type: ignore[arg-type]
        return wms

    # ─────────────────────────────────────────────────────────────────
    # Event journal (append-only, source de vérité dédup)
    # ─────────────────────────────────────────────────────────────────

    async def append_event(self, envelope: EventEnvelope) -> bool:
        """
        Append-only avec déduplication par ``event_id``.

        Returns:
            ``True`` si l'event a été persistée pour la première fois.
            ``False`` si un objet portant le même ``event_id`` existe déjà
            sous ``events/`` — dans ce cas, AUCUN write n'est fait (no-op).
        """
        if await self.has_event(envelope.event_id):
            return False
        key = layout.event_key(
            self._space_id, envelope.created_at, envelope.event_id
        )
        await self._put_model(key, envelope)
        return True

    async def has_event(self, event_id: str) -> bool:
        """
        True si un objet ``events/*_{event_id}.json`` existe déjà.

        Comme le journal est trié par timestamp, on doit LIST le préfixe
        ``events/`` puis filtrer côté client. Pour les volumes V1 (< 10⁴
        events par space), c'est acceptable. Une politique de compaction
        régulière borne la croissance.
        """
        objects = await self._storage.list_objects(layout.event_prefix(self._space_id))
        suffix = f"_{event_id}.json"
        return any(obj["Key"].endswith(suffix) for obj in objects)

    async def get_event(self, event_id: str) -> Optional[EventEnvelope]:
        """
        Retourne l'event existant pour ``event_id`` si présent.

        Utilisé par la couche peer (#4) pour distinguer un replay idempotent
        (même event_id, même payload canonique) d'un conflit de replay
        (même event_id, payload divergent).
        """
        objects = await self._storage.list_objects(layout.event_prefix(self._space_id))
        suffix = f"_{event_id}.json"
        matches = sorted(obj for obj in objects if obj["Key"].endswith(suffix))
        if not matches:
            return None
        model = await self._get_model(matches[0]["Key"], EventEnvelope)
        return model  # type: ignore[return-value]

    async def list_events(
        self,
        since_ts: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[EventEnvelope]:
        """
        Liste les events dans l'ordre lexicographique (= chronologique
        grâce au préfixe ISO 8601 dans la clé).
        """
        objects = await self._storage.list_objects(layout.event_prefix(self._space_id))
        events: list[EventEnvelope] = []
        for obj in sorted(objects, key=lambda o: o["Key"]):
            if since_ts and obj["Key"].split("/")[-1] < since_ts.replace(":", "-"):
                continue
            model = await self._get_model(obj["Key"], EventEnvelope)
            if model is not None:
                events.append(model)  # type: ignore[arg-type]
                if limit is not None and len(events) >= limit:
                    break
        return events

    async def compact_events_before(self, cutoff_iso: str) -> int:
        """
        Supprime les events dont le timestamp est strictement antérieur à
        ``cutoff_iso`` (ISO 8601). C'est l'opération de rétention.

        ⚠️ Le caller doit s'être assuré qu'il ne casse pas une chaîne
        d'ACKs en attente — cette fonction n'inspecte pas les ACKs.
        Politique recommandée : ``cutoff = min(watermark.bank_version)``
        traduit en horodatage de commit, cf. HIVEMIND_STATE.md §rétention.

        Returns:
            Nombre d'events supprimés.
        """
        deleted = 0
        cutoff_safe = cutoff_iso.replace(":", "-")
        objects = await self._storage.list_objects(layout.event_prefix(self._space_id))
        for obj in objects:
            ts_filename = obj["Key"].split("/")[-1]
            if ts_filename < cutoff_safe:
                await self._storage.delete(obj["Key"])
                deleted += 1
        return deleted

    # ─────────────────────────────────────────────────────────────────
    # Snapshot complet (reload après restart)
    # ─────────────────────────────────────────────────────────────────

    async def load_snapshot(self) -> HivemindStateSnapshot:
        """
        Charge tout l'état d'un space en un objet immuable.

        Utilisé au démarrage d'un nœud pour reconstruire sa vue protocole.
        Une erreur de corruption sur n'importe quel sous-fichier propage
        une ``CorruptedStateError`` au caller — c'est volontaire.
        """
        node = await self.get_node_identity()
        node_status = await self.get_node_status()
        members = await self.get_membership()
        term = await self.get_term()
        token = await self.get_token()
        pointer = await self.get_bank_version_pointer()
        queue = await self.list_queue()
        commits = await self.list_commits()
        tombs = await self.list_tombstones()
        watermarks = await self.list_watermarks()

        # On ne charge pas tous les events ici — peut être volumineux.
        # On donne juste le count pour le sanity check au démarrage.
        event_objects = await self._storage.list_objects(
            layout.event_prefix(self._space_id)
        )

        return HivemindStateSnapshot(
            space_id=self._space_id,
            node=node,
            node_status=node_status,
            membership=members,
            term=term,
            token=token,
            bank_version_pointer=pointer,
            queue=queue,
            commits=commits,
            tombstones=tombs,
            watermarks=watermarks,
            known_event_count=len(event_objects),
        )

    # ─────────────────────────────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────────────────────────────

    async def initialize(
        self,
        identity: NodeIdentity,
        initial_members: Optional[Iterable[Member]] = None,
    ) -> HivemindStateSnapshot:
        """
        Bootstrap d'un space vierge : pose l'identité locale et une
        membership initiale (epoch 0). Idempotent — appel répété OK.
        """
        existing_view = await self.get_membership()
        # Rotation de public_key INTERDITE via initialize, vérifiée AVANT de
        # toucher node.json : si le self-member existant a une clé NON-VIDE
        # différente, réécrire node.json (set_node_identity ci-dessous) créerait
        # une divergence durable node.json/members.json au même epoch (export
        # rejette, peer-verify utilise l'ancienne clé). Une vraie rotation passe
        # par une mutation explicite qui bumpe l'epoch (hors scope V1).
        if existing_view is not None:
            self_member = next(
                (m for m in existing_view.members if m.node_id == identity.node_id),
                None,
            )
            if (
                self_member is not None
                and self_member.public_key
                and self_member.public_key != identity.public_key
            ):
                raise RuntimeError(
                    "initialize refusé : rotation de public_key non supportée "
                    f"pour {identity.node_id!r} (node.json et members.json "
                    "divergeraient au même epoch)"
                )

        await self.set_node_identity(identity)

        if existing_view is None:
            members_list = list(initial_members or [])
            self_idx = next(
                (
                    i
                    for i, m in enumerate(members_list)
                    if m.node_id == identity.node_id
                ),
                None,
            )
            if self_idx is None:
                members_list.insert(
                    0,
                    Member(
                        node_id=identity.node_id,
                        display_name=identity.display_name,
                        public_key=identity.public_key,
                    ),
                )
            else:
                # Normaliser le self-member fourni : sa public_key DOIT provenir
                # de l'identité locale (même node). Sinon un self-member sans clé
                # (ou avec une clé stale) rendrait le space inexportable.
                existing = members_list[self_idx]
                members_list[self_idx] = existing.model_copy(
                    update={
                        "public_key": identity.public_key,
                        "display_name": existing.display_name or identity.display_name,
                    }
                )
            await self.set_membership(MembershipView(epoch=0, members=members_list))
        else:
            # Ré-initialisation d'un space DÉJÀ initialisé : ne REMPLIR qu'une
            # public_key VIDE (legacy/upgrade) du self-member, depuis l'identité.
            # On ne ROTATIONNE JAMAIS silencieusement une clé non-vide au même
            # epoch : les peers fencent les changements de membership par epoch,
            # donc une rotation de clé doit passer par une mutation explicite qui
            # bumpe l'epoch (hors scope V1). Remplir une clé absente est sûr (rien
            # n'était partagé à cet epoch pour ce champ).
            members = list(existing_view.members)
            self_idx = next(
                (
                    i
                    for i, m in enumerate(members)
                    if m.node_id == identity.node_id
                ),
                None,
            )
            if (
                self_idx is not None
                and not members[self_idx].public_key
                and identity.public_key
            ):
                members[self_idx] = members[self_idx].model_copy(
                    update={"public_key": identity.public_key}
                )
                await self.set_membership(
                    MembershipView(epoch=existing_view.epoch, members=members)
                )

        if await self.get_term() is None:
            await self._put_model(
                layout.term_key(self._space_id),
                TermState(term=0, updated_by_node_id=identity.node_id),
            )

        return await self.load_snapshot()
