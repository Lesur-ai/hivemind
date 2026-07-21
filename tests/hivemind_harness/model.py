# -*- coding: utf-8 -*-
"""
ProtocolModel — modèle de référence V1 (issue #11), TEST-ONLY.

Encode la sémantique V1 (HIVEMIND.md §5.3, §6) en pilotant les VRAIES
primitives ``HivemindStateStore`` + ``HivemindPeerChannel`` :

    QUEUE_REQUEST (FIFO, tri déterministe)
      → all-ACK de persistance
      → grant unique (bump term, token HELD avec fencing_token == term, lease)
      → commit borné (gardé par le fencing à l'application)
      → release.

Pourquoi un modèle ici plutôt que les vrais services : les services #6/#7/#8/
#12 N'EXISTENT PAS encore. Le modèle porte donc la sémantique pour que #11
soit un vrai gate dès maintenant. Il reste strictement test-only et minimal ;
les invariants sont vérifiés contre l'état RÉEL, pas contre les internes du
modèle. Quand les services réels arrivent, ils remplacent chaque pas du modèle
derrière les mêmes seams (NodeRuntime), sans changer les invariants.

Mappings test-layer (NE PAS ajouter à src/ EventType — cf. apiGaps #8/#10/#12) :

- QUEUE_REQUEST  → EventType.TOKEN_CLAIM (le claim double comme queue-request).
- resync / eviction / lease-expiry → concepts modèle (pas d'EventType prod).
"""

from __future__ import annotations

from enum import Enum

from live_mem.core.hivemind import (
    Ack,
    BankCommit,
    BankVersionPointer,
    EventEnvelope,
    EventType,
    PeerChannelError,
    QueueEntry,
    QueueEntryStatus,
    TokenLeaseState,
    TokenState,
    Tombstone,
    canonical_event_payload_hash,
)

from .cluster import ClusterHarness


class HiveStatus(str, Enum):
    """Statut opérateur dérivé (HIVEMIND.md §6.4), calculé sur l'état réel."""

    DISABLED = "disabled"
    HEALTHY = "healthy"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    UNSAFE = "unsafe"
    RESYNC_REQUIRED = "resync_required"


# Erreur modèle pour les rejets de fencing à l'APPLICATION du commit (rôle du
# futur #8). Distincte de PeerChannelError (rejets au transport) et de
# RuntimeError (gardes du store).
class CommitFencedError(RuntimeError):
    """Levée par le modèle quand un commit est fencé AVANT append_commit."""


class ProtocolModel:
    """
    Pilote V1 déterministe au-dessus d'un ``ClusterHarness``.

    Le modèle attribue les ``sequence`` de queue (allocateur absent en prod,
    cf. apiGaps) avec un tri déterministe : (sequence demandée, event_id) afin
    que des claims concurrents soient sérialisés de façon reproductible sur
    tous les nœuds.
    """

    def __init__(self, cluster: ClusterHarness, *, lease_seconds: int = 300) -> None:
        self.cluster = cluster
        self.lease_seconds = lease_seconds
        # Allocateur de sequence déterministe (le head de queue gagne).
        self._next_sequence = 0

    # ─────────────────────────────────────────────────────────────────
    # Étape 1 — QUEUE_REQUEST (claim) : enqueue + broadcast + persist
    # ─────────────────────────────────────────────────────────────────

    def allocate_sequence(self) -> int:
        seq = self._next_sequence
        self._next_sequence += 1
        return seq

    async def claim(
        self,
        requester: str,
        *,
        event_id: str,
        sequence: int | None = None,
        deliver: bool = True,
    ) -> EventEnvelope:
        """
        Émet un QUEUE_REQUEST (mappé sur TOKEN_CLAIM) :

        1. enfile localement chez le requester (queue durable + event) ;
        2. diffuse l'event signé aux pairs (en transit jusqu'à livraison) ;
        3. si ``deliver``, livre tout (les pairs persistent puis pourront ACK).

        Retourne l'``EventEnvelope`` du claim.
        """
        if sequence is None:
            sequence = self.allocate_sequence()
        node = self.cluster.nodes[requester]
        event = EventEnvelope(
            event_id=event_id,
            request_id=event_id,
            type=EventType.TOKEN_CLAIM,
            origin_node_id=requester,
            term=self.cluster.term,
            membership_epoch=self.cluster.epoch,
            payload={"kind": "queue_request", "sequence": sequence},
            created_at=self.cluster.clock.iso(),
        )
        # Persistance locale du requester (event + entrée de queue durable).
        await node.store.append_event(event)
        await node.store.enqueue(
            QueueEntry(
                event_id=event_id,
                request_id=event_id,
                sequence=sequence,
                requester_node_id=requester,
                term=self.cluster.term,
                membership_epoch=self.cluster.epoch,
            )
        )
        await self.cluster.broadcast(requester, event)
        if deliver:
            await self.cluster.transport.deliver_all()
            # Tout nœud ayant persisté le claim DOIT dériver la MÊME entrée de
            # queue (HIVEMIND.md §5.3 : « all peers derive the same queue order
            # from the same events »). La vraie ``channel.receive`` ne persiste
            # que l'event ; cette dérivation queue est le rôle du futur
            # QueueService (#6), porté ici par le modèle.
            await self.replicate_queue_entries()
        return event

    # ─────────────────────────────────────────────────────────────────
    # Réplication de la queue : chaque nœud dérive la MÊME queue des events
    # ─────────────────────────────────────────────────────────────────

    async def replicate_queue_entries(self) -> None:
        """
        Garantit que chaque nœud actif possède une ``QueueEntry`` pour CHAQUE
        event ``TOKEN_CLAIM`` qu'il a persisté localement (HIVEMIND.md §5.3 :
        tous les pairs dérivent le MÊME ordre de queue des MÊMES events).

        Sans ça, seul le requester enfile une entrée : un pair qui reçoit le
        claim signé ne persiste (via ``channel.receive``) QUE l'event, pas
        l'entrée de queue. La garde head-of-queue de ``grant`` lit la queue
        LOCALE du holder ; si elle est incomplète, un grant hors-ordre
        distribué (accorder une sequence plus tardive avant une plus ancienne
        détenue par un autre nœud) passe à tort.

        Déterministe et idempotent : la clé S3 de l'entrée est dérivée de
        ``(sequence, event_id)`` (cf. ``enqueue``) ; ré-écrire la même entrée
        produit le même objet, donc une re-livraison / un doublon ne
        double-enfile jamais. Une entrée déjà présente (même event_id) n'est PAS
        ré-écrite, pour ne pas écraser un status consommé (GRANTED/CANCELLED).
        Le rôle réel revient au QueueService (#6) ; ici le modèle le porte.
        """
        for nid in self.cluster.node_ids():
            store = self.cluster.nodes[nid].store
            existing_ids = {e.event_id for e in await store.list_queue()}
            for event in await store.list_events():
                if event.type != EventType.TOKEN_CLAIM.value:
                    continue
                if event.event_id in existing_ids:
                    continue  # déjà enfilé (idempotence / status préservé)
                payload = event.payload or {}
                if payload.get("kind") != "queue_request":
                    continue
                await store.enqueue(
                    QueueEntry(
                        event_id=event.event_id,
                        request_id=event.request_id or event.event_id,
                        sequence=int(payload["sequence"]),
                        requester_node_id=event.origin_node_id,
                        term=event.term,
                        membership_epoch=event.membership_epoch,
                    )
                )

    # ─────────────────────────────────────────────────────────────────
    # Étape 2 — ACK de persistance par les pairs
    # ─────────────────────────────────────────────────────────────────

    async def ack(self, acker: str, *, event_id: str, to_holder: str) -> bool:
        """
        Le pair ``acker`` ACK l'event SI ET SEULEMENT SI il l'a persisté en
        local (HIVEMIND.md §6.1 : un pair n'ACK qu'après l'écriture durable de
        l'event dans son journal). Sans cette garde, un ACK pourrait être
        enregistré pour un pair qui n'a JAMAIS reçu le claim (deliver=False /
        drop / partition), rendant ``can_grant`` vrai à tort et masquant le
        scénario de livraison perdue que le gate all-ACK doit attraper.

        Si l'ACK est légitime, il est écrit sur le storage du holder (qui agrège
        les ACK pour la décision all-ACK) et la méthode retourne ``True``. Si le
        pair n'a pas persisté l'event, AUCUN ACK n'est enregistré et la méthode
        retourne ``False`` pour que le caller voie le saut.
        """
        acker_store = self.cluster.nodes[acker].store
        if not await acker_store.has_event(event_id):
            # Le pair n'a pas (encore) persisté l'event : pas d'ACK possible.
            return False
        holder_store = self.cluster.nodes[to_holder].store
        await holder_store.record_ack(
            Ack(
                event_id=event_id,
                ack_by_node_id=acker,
                term=self.cluster.term,
                membership_epoch=self.cluster.epoch,
            )
        )
        return True

    async def collect_acks(
        self, event_id: str, *, holder: str, ackers: list[str] | None = None
    ) -> int:
        """
        Chaque pair ayant PERSISTÉ l'event ACK vers le holder. ``ackers`` permet
        de restreindre l'ensemble des pairs sollicités (scénario d'ACK perdu /
        partition). Un pair qui n'a pas reçu+persisté le claim ne produit AUCUN
        ACK (cf. ``ack``), donc une livraison perdue laisse naturellement
        all-ACK insatisfait. Le holder s'auto-ACK puisqu'il a persisté en local
        au moment du ``claim``.

        Retourne le nombre d'ACK distincts effectivement enregistrés sur le
        holder.
        """
        if ackers is None:
            ackers = self.cluster.node_ids()
        for acker in ackers:
            await self.ack(acker, event_id=event_id, to_holder=holder)
        return len(await self.cluster.nodes[holder].store.list_acks(event_id))

    async def can_grant(self, event_id: str, *, holder: str) -> bool:
        """Le grant n'est autorisé QUE si all-ACK est satisfait (V1)."""
        return await self.cluster.all_acked(event_id, on_node=holder)

    # ─────────────────────────────────────────────────────────────────
    # Étape 3 — grant unique (bump term, token HELD, lease)
    # ─────────────────────────────────────────────────────────────────

    def _queue_head(self, entries: list[QueueEntry]) -> QueueEntry | None:
        """
        Head déterministe de la queue parmi les entrées PENDING : tri
        ``(sequence, event_id)``. Les entrées GRANTED/CANCELLED sont consommées
        et n'ont plus le droit de tête. Retourne ``None`` si rien n'est pending.
        """
        pending = [
            e for e in entries if e.status == QueueEntryStatus.PENDING.value
        ]
        if not pending:
            return None
        return min(pending, key=lambda e: (e.sequence, e.event_id))

    async def _active_lease_holder(self) -> tuple[str, str, int] | None:
        """
        Cherche, à travers TOUS les stores du cluster, un token actif
        (HELD/RELEASING) — détenu par N'IMPORTE QUEL nœud, Y COMPRIS le
        requérant lui-même — dont la lease n'est PAS expirée à l'horloge logique
        courante (``self.cluster.clock.iso()``).

        C'est la précondition d'exclusion mutuelle V1 (HIVEMIND.md §5.3/§6.2) :
        le cycle est strictement claim → grant → commit → release. Tant qu'un
        détenteur (peu importe lequel) tient une lease non expirée, aucun grant
        ne peut produire un SECOND détenteur valide. La lease du requérant
        lui-même compte AUSSI : sans elle dans le périmètre, un holder soumettant
        une SECONDE entrée queue avant de relâcher sa première lease se verrait
        accorder un nouveau token et bumper le term, faisant chevaucher deux
        consolidations par un même détenteur (gate affaibli). Un re-grant de la
        MÊME entrée reste, lui, attrapé en amont par la garde PENDING/head-of-
        queue (ordre des gardes dans ``grant``), pas par cette exclusion. Le
        token FREE (après ``release``) et la lease expirée (horloge avancée
        au-delà de ``lease_until``) ne bloquent PAS — c'est par eux que progresse
        le grant suivant.

        Retourne ``(node_id_du_store, holder_node_id, term)`` du premier
        détenteur actif non expiré trouvé (itération par ``node_id`` trié, donc
        déterministe), ou ``None`` si aucun.
        """
        now_iso = self.cluster.clock.iso()
        for nid in self.cluster.node_ids():
            token = await self.cluster.nodes[nid].store.get_token()
            if token is None:
                continue
            if token.state not in {
                TokenState.HELD.value,
                TokenState.RELEASING.value,
            }:
                continue
            if self.is_lease_expired(token, now_iso):
                continue  # lease expirée -> ne bloque pas le grant suivant
            return (nid, token.holder_node_id or "", token.term)
        return None

    async def grant(self, holder: str, *, event_id: str) -> TokenLeaseState:
        """
        Accorde le token au HEAD de queue SOUS all-ACK strict.

        Gardes (dans l'ordre), AVANT tout effet de bord :

        1. all-ACK satisfait (sinon ``RuntimeError`` — pas de progrès silencieux,
           le non-goal quorum est protégé ici) ;
        2. ``event_id`` est le HEAD déterministe ``(sequence, event_id)`` de la
           queue du holder ET il est encore PENDING. Un grant hors-tête ou
           d'une entrée déjà accordée est rejeté (``RuntimeError``) — sinon une
           requête plus tardive pourrait être accordée dans le désordre, ou la
           même entrée accordée deux fois. Cette garde (lecture seule, sans
           consommation) passe AVANT l'exclusion mutuelle pour que le re-grant de
           la MÊME entrée déjà accordée soit attrapé par sa cause première (entrée
           non-PENDING), pas masqué par la lease active du holder ;
        3. exclusion mutuelle : AUCUN nœud — Y COMPRIS le ``holder`` lui-même —
           ne tient une lease active (token HELD/RELEASING) NON expirée à
           l'horloge logique courante. Sans cette garde, un second claim all-ACKé
           pendant qu'un détenteur tient encore le token produirait un second
           détenteur valide (split-brain à un term supérieur) ; et si le holder
           courant soumet une SECONDE entrée queue avant de relâcher sa première
           lease, il chevaucherait deux consolidations sous un même détenteur. Le
           prochain grant ne peut avancer qu'après un ``release`` (token FREE) OU
           une expiration de lease (horloge au-delà de ``lease_until``).

        Effets (seulement si toutes les gardes passent) :

        - l'entrée de queue passe à GRANTED (consommée — un second grant la
          verra non-PENDING et sera rejeté) ;
        - bump du term (nouveau term = grant) ;
        - écriture token HELD avec ``fencing_token == term`` (invariant modèle
          Pydantic), lease bornée par l'horloge logique.
        """
        if not await self.can_grant(event_id, holder=holder):
            raise RuntimeError(
                f"grant refusé: all-ACK non satisfait pour event {event_id!r} "
                f"(un membre actif n'a pas ACKé) — V1 bloque, ne progresse pas"
            )

        store = self.cluster.nodes[holder].store

        # --- Garde head-of-queue + PENDING (lecture seule, anti out-of-order /
        # double-grant) ---
        # AVANT l'exclusion mutuelle : un re-grant de la MÊME entrée déjà
        # accordée doit échouer sur « n'est plus PENDING » (sa cause première),
        # pas sur la lease active du holder. La consommation est différée APRÈS
        # l'exclusion mutuelle pour ne produire AUCUN effet de bord en cas de
        # rejet.
        queue = await store.list_queue()
        target = next((e for e in queue if e.event_id == event_id), None)
        if target is None:
            raise RuntimeError(
                f"grant refusé: event {event_id!r} absent de la queue de "
                f"{holder!r} — rien à accorder"
            )
        if target.status != QueueEntryStatus.PENDING.value:
            raise RuntimeError(
                f"grant refusé: event {event_id!r} n'est plus PENDING "
                f"(status={target.status}) sur {holder!r} — déjà accordé/annulé "
                f"(double-grant interdit)"
            )
        head = self._queue_head(queue)
        assert head is not None  # target est PENDING, donc il existe un head.
        if head.event_id != event_id:
            raise RuntimeError(
                f"grant refusé: event {event_id!r} (sequence={target.sequence}) "
                f"n'est pas le head de queue (head={head.event_id!r} "
                f"sequence={head.sequence}) sur {holder!r} — grant hors-ordre "
                f"interdit"
            )

        # --- Garde d'exclusion mutuelle (lease active non expirée) ---
        # AVANT toute consommation de queue / bump de term / écriture de token :
        # un détenteur encore actif et non expiré — Y COMPRIS le holder lui-même
        # avec une lease pas encore relâchée — interdit un nouveau grant. La
        # lease expirée et le token FREE laissent passer (cf. test
        # lease-expiry-then-higher-term + flow nominal release -> grant).
        active = await self._active_lease_holder()
        if active is not None:
            store_nid, current_holder, active_term = active
            raise RuntimeError(
                f"grant refusé: lease active détenue par {current_holder!r} au "
                f"term {active_term} (store {store_nid!r}), non expirée — un "
                f"second détenteur valide est interdit (V1 mutual-exclusion). "
                f"Attendre release (token FREE) ou expiration de lease"
            )

        # Consommation : l'entrée passe GRANTED chez le holder ET chez tous les
        # nœuds actifs qui ont dérivé la même entrée (modélise la diffusion
        # TOKEN_GRANTED : chaque pair marque la requête accordée consommée dans
        # SA queue dérivée). Sans ça, l'entrée resterait PENDING ailleurs et le
        # head distribué divergerait après un grant — la queue répliquée doit
        # rester cohérente (HIVEMIND.md §5.3). Idempotent : ré-écrire le même
        # status GRANTED produit le même objet S3.
        for nid in self.cluster.node_ids():
            peer_store = self.cluster.nodes[nid].store
            peer_entry = next(
                (e for e in await peer_store.list_queue() if e.event_id == event_id),
                None,
            )
            if (
                peer_entry is not None
                and peer_entry.status == QueueEntryStatus.PENDING.value
            ):
                await peer_store.update_queue_entry_status(
                    peer_entry, QueueEntryStatus.GRANTED
                )
        # --- Fin consommation de queue ---

        new_term = self.cluster.term + 1
        await store.bump_term(new_term, updated_by_node_id=holder)
        self.cluster.term = new_term

        granted_at = self.cluster.clock.iso()
        lease_until = self.cluster.clock.now().replace(microsecond=0)
        from datetime import timedelta

        lease_until = (lease_until + timedelta(seconds=self.lease_seconds)).isoformat()

        token = TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id=holder,
            term=new_term,
            fencing_token=new_term,
            granted_at=granted_at,
            lease_until=lease_until,
            membership_epoch=self.cluster.epoch,
            event_id=event_id,
        )
        await store.set_token(token)
        return token

    # ─────────────────────────────────────────────────────────────────
    # Étape 4 — commit borné, GARDÉ par le fencing (rôle du futur #8)
    # ─────────────────────────────────────────────────────────────────

    async def apply_commit(
        self,
        holder: str,
        *,
        bank_version: int,
        commit_id: str,
        commit_term: int | None = None,
    ) -> BankCommit:
        """
        Applique un commit de bank borné, APRÈS vérification du fencing.

        IMPORTANT : ``append_commit`` du store NE valide PAS le term/fencing
        (cf. apiGaps). Le garde-fou de fencing à l'application est donc ICI (le
        futur #8) : un commit dont le ``term`` n'est pas le term courant du
        store / le fencing_token du token HELD est rejeté AVANT que
        ``append_commit`` ne soit jamais appelé.

        Retourne le ``BankCommit`` persisté. Lève ``CommitFencedError`` si le
        commit est stale.
        """
        store = self.cluster.nodes[holder].store
        commit_term = self.cluster.term if commit_term is None else commit_term

        # --- Garde de fencing à l'application (le « commit-apply guard » #8) ---
        term_state = await store.get_term()
        current_term = term_state.term if term_state else 0
        token = await store.get_token()
        if commit_term < current_term:
            raise CommitFencedError(
                f"commit fencé: term={commit_term} < term courant="
                f"{current_term} sur {holder!r} — rejeté AVANT append_commit"
            )
        if token is None or token.state != TokenState.HELD.value:
            raise CommitFencedError(
                f"commit fencé: {holder!r} ne tient pas le token (state="
                f"{token.state if token else None}) — rejeté AVANT append_commit"
            )
        if token.holder_node_id != holder or token.fencing_token != commit_term:
            raise CommitFencedError(
                f"commit fencé: fencing_token={token.fencing_token} != "
                f"term commit={commit_term} ou holder mismatch sur {holder!r} "
                f"— rejeté AVANT append_commit"
            )
        # --- Fin garde fencing ; append_commit n'est atteint que si OK ---

        pointer = await store.get_bank_version_pointer()
        parent = pointer.bank_version if pointer else -1
        commit = BankCommit(
            bank_version=bank_version,
            parent_bank_version=parent,
            term=commit_term,
            membership_epoch=self.cluster.epoch,
            commit_id=commit_id,
            committed_by_node_id=holder,
        )
        persisted = await store.append_commit(commit)
        await store.set_bank_version_pointer(
            BankVersionPointer(bank_version=bank_version, commit_id=commit_id)
        )
        return persisted

    # ─────────────────────────────────────────────────────────────────
    # Étape 5 — release
    # ─────────────────────────────────────────────────────────────────

    async def release(self, holder: str) -> TokenLeaseState:
        """Libère le token (FREE), conservant term/fencing pour la monotonie."""
        store = self.cluster.nodes[holder].store
        token = await store.get_token()
        assert token is not None, "release sans token"
        freed = TokenLeaseState(
            state=TokenState.FREE,
            holder_node_id=None,
            term=token.term,
            fencing_token=token.fencing_token,
            membership_epoch=self.cluster.epoch,
        )
        await store.set_token(freed)
        return freed

    # ─────────────────────────────────────────────────────────────────
    # Réconciliation d'un holder stale après montée du term
    # ─────────────────────────────────────────────────────────────────

    async def reconcile_stale_holder(self, node_id: str) -> TokenLeaseState | None:
        """
        Réconcilie le token LOCAL d'un nœud qui a appris un term supérieur
        (HIVEMIND.md §6.2/§6.3 : un holder superseded/expiré ne reste JAMAIS un
        HELD silencieux ; il devient stale/blocked, jamais un second holder
        valide cohabitant avec le nouveau).

        Contrat : si le token local est ``HELD``/``RELEASING`` à un ``term``
        STRICTEMENT inférieur au term courant du store (le nœud a vu un
        ``TERM_BUMPED`` / ``TOKEN_GRANTED`` plus récent), on le sort de l'état
        actif en écrivant un token ``FREE`` au term COURANT du store. Cela :

        - retire le holder stale de l'ensemble {HELD, RELEASING} (plus de
          divergence cross-term silencieuse) ;
        - préserve la monotonicité term/fencing exigée par ``set_token`` (on ne
          descend jamais : on monte fencing/term au term courant) ;
        - laisse le commit-apply guard fencer toute tentative de commit de
          l'ancien holder à son ancien term (le fencing reste effectif).

        Idempotent : si le token est déjà FREE, ou à jour du term courant, ou
        absent, c'est un no-op (retourne le token courant ou ``None``).
        Retourne le token réconcilié (ou l'état inchangé).
        """
        store = self.cluster.nodes[node_id].store
        token = await store.get_token()
        if token is None:
            return None
        term_state = await store.get_term()
        current_term = term_state.term if term_state else 0
        if token.state not in {TokenState.HELD.value, TokenState.RELEASING.value}:
            return token  # déjà non-actif : rien à réconcilier
        if token.term >= current_term:
            return token  # holder courant légitime au term courant : on garde
        # Holder stale (term < term courant) : on le sort de l'état actif. FREE
        # au term courant -> fencing/ term montent (jamais descendre), holder
        # vidé. Le nouveau holder reste le seul valide au term max.
        reconciled = TokenLeaseState(
            state=TokenState.FREE,
            holder_node_id=None,
            term=current_term,
            fencing_token=current_term,
            membership_epoch=self.cluster.epoch,
        )
        await store.set_token(reconciled)
        return reconciled

    # ─────────────────────────────────────────────────────────────────
    # Helpers de fencing / lease / resync (concepts modèle)
    # ─────────────────────────────────────────────────────────────────

    @staticmethod
    def is_lease_expired(token: TokenLeaseState, now_iso: str) -> bool:
        """Vrai si l'horloge logique a dépassé ``lease_until``."""
        if token.lease_until is None:
            return False
        from datetime import datetime

        def _parse(value: str) -> datetime:
            from datetime import timezone

            p = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return p if p.tzinfo else p.replace(tzinfo=timezone.utc)

        return _parse(now_iso) > _parse(token.lease_until)

    async def cluster_committed_bank_version(self) -> int:
        """
        bank_version committé par le CLUSTER : le max des pointeurs
        ``bank_version`` observés sur l'ensemble des nœuds. Un nœud dont le
        pointeur local est strictement inférieur est en retard (resync requis).

        On prend le max des pointeurs (et non des commits matérialisés) parce
        que le pointeur est l'engagement local appliqué ; un commit présent sur
        un seul nœud sans pointeur avancé n'est pas encore « le cluster ».
        ``-1`` si aucun nœud n'a de pointeur valide.
        """
        committed = -1
        for nid in self.cluster.node_ids():
            pointer = await self.cluster.nodes[nid].store.get_bank_version_pointer()
            if pointer is not None and pointer.bank_version > committed:
                committed = pointer.bank_version
        return committed

    async def hive_status(self, node_id: str, *, pending_event: str | None = None) -> HiveStatus:
        """
        Statut opérateur dérivé de l'état RÉEL (HIVEMIND.md §6.4).

        - UNSAFE : un fichier critique est corrompu (CorruptedStateError au
          load) — déterminé par le caller, ici on reflète via une exception.
        - RESYNC_REQUIRED : le nœud est en retard sur le cluster, SOIT par
          ``epoch`` (membership), SOIT par ``bank_version`` committé (pointeur
          local strictement inférieur au max committé du cluster). Comparer
          uniquement l'epoch raterait un nœud à jour d'epoch mais en retard de
          commit.
        - BLOCKED : un event en attente (``pending_event``) n'a pas atteint
          all-ACK.
        - HEALTHY sinon.
        """
        node = self.cluster.nodes[node_id]
        store = node.store

        # Resync (1) : retard d'epoch de membership.
        view = await store.get_membership()
        if view is not None and view.epoch < self.cluster.epoch:
            return HiveStatus.RESYNC_REQUIRED

        # Resync (2) : retard de bank_version committé vs le cluster. Un nœud à
        # jour d'epoch mais dont le pointeur committé est en arrière DOIT se
        # resynchroniser avant de se croire HEALTHY.
        cluster_committed = await self.cluster_committed_bank_version()
        local_pointer = await store.get_bank_version_pointer()
        local_committed = local_pointer.bank_version if local_pointer else -1
        if local_committed < cluster_committed:
            return HiveStatus.RESYNC_REQUIRED

        if pending_event is not None:
            satisfied = await self.cluster.all_acked(pending_event, on_node=node_id)
            if not satisfied:
                return HiveStatus.BLOCKED

        return HiveStatus.HEALTHY

    async def add_tombstone(
        self, node_id: str, *, note_id: str, bank_version: int
    ) -> Tombstone:
        store = self.cluster.nodes[node_id].store
        tomb = Tombstone(
            note_id=note_id,
            deleted_by_node_id=node_id,
            term=self.cluster.term,
            membership_epoch=self.cluster.epoch,
            bank_version=bank_version,
        )
        return await store.add_tombstone(tomb)

    async def is_tombstoned(self, node_id: str, *, note_id: str) -> bool:
        store = self.cluster.nodes[node_id].store
        return await store.get_tombstone(note_id) is not None

    async def replicate_note(self, node_id: str, *, note_id: str) -> bool:
        """
        Replication guard (rôle du futur #12) : un pair tente de (ré)introduire
        une live-note. Si la note est tombée localement, la résurrection est
        REFUSÉE (retourne ``False``) ; sinon elle serait acceptée (``True``).

        Modélise « delayed live-note replication after tombstone » : un message
        de réplication tardif portant une note déjà consommée + tombée ne doit
        jamais ressusciter la note.
        """
        if await self.is_tombstoned(node_id, note_id=note_id):
            return False
        return True
