# -*- coding: utf-8 -*-
"""
Tests pour issue #3 — état protocole Hivemind persistant et layout S3.

Couvre :
- sérialisation aller-retour des modèles Pydantic ;
- persistance + rechargement après "redémarrage" (nouvelle instance du store) ;
- déduplication déterministe de l'append-only event journal ;
- ordre FIFO de la queue (lexicographique sur ``sequence`` zero-padded) ;
- détection d'une corruption (JSON cassé / schéma invalide).
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from live_mem.core.hivemind import (
    Ack,
    BankCommit,
    BankCommitManifestEntry,
    BankVersionPointer,
    CorruptedStateError,
    EventEnvelope,
    EventType,
    HivemindStateStore,
    Member,
    MembershipView,
    NodeIdentity,
    PROTOCOL_VERSION,
    QueueEntry,
    QueueEntryStatus,
    TermState,
    TokenLeaseState,
    TokenState,
    Tombstone,
    Watermark,
    layout,
)


# =============================================================================
# Fake storage — émule StorageService pour des tests déterministes sans S3
# =============================================================================


class FakeStorage:
    """
    Implémentation in-memory minimale du contrat ``StorageService`` utilisé
    par ``HivemindStateStore``.

    Le but n'est pas de couvrir 100 % de l'API de ``StorageService`` mais
    juste les méthodes que le store appelle : ``put_json``, ``put``,
    ``get``, ``delete``, ``list_objects``, ``exists``.
    """

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        # Hooks de test
        self.put_calls = 0
        self.get_calls = 0
        self.delete_calls = 0

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.put_calls += 1
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        return self.objects.get(key)

    async def get_json(self, key: str) -> dict | None:
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def delete(self, key: str) -> None:
        self.delete_calls += 1
        self.objects.pop(key, None)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        out: list[dict] = []
        for key in sorted(self.objects):
            if key.startswith(prefix):
                out.append({"Key": key, "Size": len(self.objects[key]), "LastModified": ""})
                if max_keys and len(out) >= max_keys:
                    break
        return out

    async def exists(self, key: str) -> bool:
        return key in self.objects

    def snapshot(self) -> dict[str, str]:
        """Copie défensive — utile pour vérifier qu'un appel n'écrit rien."""
        return deepcopy(self.objects)


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def store(storage: FakeStorage) -> HivemindStateStore:
    return HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]


# =============================================================================
# Layout — clés et invariants de format
# =============================================================================


class TestLayout:
    def test_hivemind_prefix_basic(self) -> None:
        assert layout.HIVEMIND_PREFIX("alpha") == "alpha/_hivemind/"

    def test_space_id_validation(self) -> None:
        with pytest.raises(ValueError):
            layout.HIVEMIND_PREFIX("")
        with pytest.raises(ValueError):
            layout.HIVEMIND_PREFIX("a/b")

    def test_singleton_keys(self) -> None:
        assert layout.node_key("alpha") == "alpha/_hivemind/node.json"
        assert layout.members_key("alpha") == "alpha/_hivemind/members.json"
        assert layout.term_key("alpha") == "alpha/_hivemind/term.json"
        assert layout.token_key("alpha") == "alpha/_hivemind/token.json"
        assert layout.bank_version_key("alpha") == "alpha/_hivemind/bank_version.json"

    def test_queue_key_zero_padded_preserves_order(self) -> None:
        k1 = layout.queue_entry_key("alpha", 1, "evt1")
        k9 = layout.queue_entry_key("alpha", 9, "evt9")
        k10 = layout.queue_entry_key("alpha", 10, "evt10")
        # Ordre lexicographique == ordre numérique grâce au zero-padding
        assert sorted([k10, k1, k9]) == [k1, k9, k10]

    def test_queue_key_rejects_negative_sequence(self) -> None:
        with pytest.raises(ValueError):
            layout.queue_entry_key("alpha", -1, "evt")

    def test_event_key_replaces_colons(self) -> None:
        key = layout.event_key("alpha", "2026-05-27T12:34:56+00:00", "evtX")
        assert ":" not in key
        assert key.endswith("_evtX.json")

    def test_commit_key_orders_versions_lexically(self) -> None:
        # Le pad doit suffire à gérer des entiers larges sans réordonner.
        keys = [layout.commit_key("alpha", v) for v in [0, 1, 10, 100, 9999999999]]
        assert keys == sorted(keys)

    def test_ack_key_contains_event_and_node(self) -> None:
        key = layout.ack_key("alpha", "evtA", "nodeB")
        assert key == "alpha/_hivemind/acks/evtA/nodeB.json"


# =============================================================================
# Sérialisation Pydantic — invariants des modèles
# =============================================================================


class TestModelSerialization:
    def test_node_identity_roundtrip(self) -> None:
        ident = NodeIdentity(node_id="aaaa1111", display_name="laptop-perso")
        dumped = ident.model_dump(mode="json")
        loaded = NodeIdentity.model_validate(dumped)
        assert loaded == ident
        assert loaded.protocol_version == PROTOCOL_VERSION

    def test_node_identity_rejects_empty_id(self) -> None:
        with pytest.raises(ValueError):
            NodeIdentity(node_id="")

    def test_term_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            TermState(term=-1, updated_by_node_id="n")

    def test_bank_commit_roundtrip_with_manifest(self) -> None:
        commit = BankCommit(
            bank_version=1,
            parent_bank_version=0,
            term=3,
            membership_epoch=1,
            commit_id="commit-deadbeef",
            committed_by_node_id="nodeA",
            manifest=[
                BankCommitManifestEntry(path="activeContext.md", sha256="aaa", size=42),
                BankCommitManifestEntry(path="progress.md", sha256="bbb", size=100),
            ],
            notes_consumed=["note1", "note2"],
        )
        dumped = commit.model_dump(mode="json")
        loaded = BankCommit.model_validate(dumped)
        assert loaded == commit
        # Champs requis par l'issue #3 : présents dans le modèle
        assert "bank_version" in dumped
        assert "term" in dumped
        assert "membership_epoch" in dumped

    def test_event_envelope_carries_all_required_fields(self) -> None:
        env = EventEnvelope(
            event_id="evt-1",
            request_id="req-1",
            type=EventType.BANK_COMMITTED,
            origin_node_id="nodeA",
            term=5,
            membership_epoch=2,
            bank_version=3,
            payload={"commit_id": "c1"},
        )
        dumped = env.model_dump(mode="json")
        # Issue #3 : protocol_version, membership_epoch, term, bank_version,
        # event_id, request_id sont tous présents.
        for required in (
            "protocol_version",
            "membership_epoch",
            "term",
            "bank_version",
            "event_id",
            "request_id",
        ):
            assert required in dumped, f"champ manquant : {required}"

    @pytest.mark.parametrize(
        "model_cls, ctor_kwargs",
        [
            (
                TokenLeaseState,
                {"state": TokenState.FREE, "term": 0, "fencing_token": 0},
            ),
            (
                QueueEntry,
                {"event_id": "e", "sequence": 0, "requester_node_id": "n"},
            ),
            (
                Ack,
                {"event_id": "e", "ack_by_node_id": "n"},
            ),
            (
                Tombstone,
                {"note_id": "n1", "deleted_by_node_id": "n"},
            ),
            (
                Watermark,
                {"node_id": "n"},
            ),
            (
                BankCommit,
                {
                    "bank_version": 0,
                    "term": 0,
                    "commit_id": "c0",
                    "committed_by_node_id": "n",
                },
            ),
        ],
    )
    def test_protocol_records_carry_common_contract_fields(
        self, model_cls: type, ctor_kwargs: dict
    ) -> None:
        """
        Garde-fou contre la régression P2.1 : tout record protocole persisté
        DOIT porter les six champs communs du contrat issue #3 (au moins
        avec un défaut explicite), sinon la dédup/corrélation/fencing
        est inégale entre couches.
        """
        instance = model_cls(**ctor_kwargs)
        dumped = instance.model_dump(mode="json")
        for required in (
            "protocol_version",
            "membership_epoch",
            "term",
            "bank_version",
            "event_id",
            "request_id",
        ):
            assert required in dumped, (
                f"{model_cls.__name__} doit exposer le champ '{required}' "
                f"du contrat protocole issue #3 (cf. HIVEMIND_STATE.md)"
            )

    def test_token_lease_state_held_requires_fencing_equals_term(self) -> None:
        """Invariant : un token HELD/RELEASING doit avoir fencing_token == term."""
        # FREE : fencing_token != term toléré (peut traîner après release)
        TokenLeaseState(state=TokenState.FREE, term=5, fencing_token=0)

        # HELD : fencing_token doit == term
        TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id="nodeA",
            term=5,
            fencing_token=5,
        )
        with pytest.raises(ValueError):
            TokenLeaseState(
                state=TokenState.HELD,
                holder_node_id="nodeA",
                term=5,
                fencing_token=3,
            )

        # RELEASING : même invariant
        with pytest.raises(ValueError):
            TokenLeaseState(
                state=TokenState.RELEASING,
                holder_node_id="nodeA",
                term=5,
                fencing_token=7,
            )

    def test_event_envelope_rejects_slash_in_id(self) -> None:
        with pytest.raises(ValueError):
            EventEnvelope(
                event_id="bad/id",
                type=EventType.TOKEN_CLAIM,
                origin_node_id="nodeA",
            )

    def test_extra_fields_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            # Pydantic ConfigDict(extra="forbid")
            NodeIdentity.model_validate(
                {"node_id": "x", "unknown_field": "boom"}
            )


# =============================================================================
# Persistence + reload (acceptance criterion : "persist and reload after restart")
# =============================================================================


@pytest.mark.asyncio
class TestPersistenceAndReload:
    async def test_initialize_and_reload_with_fresh_store(
        self, storage: FakeStorage
    ) -> None:
        s1 = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
        ident = NodeIdentity(node_id="node1", display_name="local")
        await s1.initialize(ident)

        # Simule un restart : nouvelle instance, même storage
        s2 = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
        snap = await s2.load_snapshot()
        assert snap.node == ident
        assert snap.membership is not None
        assert snap.membership.epoch == 0
        assert any(m.node_id == "node1" for m in snap.membership.members)
        assert snap.term is not None and snap.term.term == 0

    async def test_initialize_is_idempotent(self, store: HivemindStateStore, storage: FakeStorage) -> None:
        ident = NodeIdentity(node_id="node1")
        await store.initialize(ident)
        # Re-init : pas d'écrasement de la membership
        await store.set_membership(MembershipView(
            epoch=5,
            members=[Member(node_id="node1"), Member(node_id="node2")],
        ))
        await store.initialize(ident)  # doit être no-op pour membership
        view = await store.get_membership()
        assert view is not None and view.epoch == 5
        assert len(view.members) == 2

    async def test_node_identity_change_is_rejected(self, store: HivemindStateStore) -> None:
        await store.set_node_identity(NodeIdentity(node_id="node1"))
        with pytest.raises(RuntimeError):
            await store.set_node_identity(NodeIdentity(node_id="node2"))

    async def test_membership_epoch_must_grow(self, store: HivemindStateStore) -> None:
        await store.set_membership(MembershipView(epoch=2))
        with pytest.raises(RuntimeError):
            await store.set_membership(MembershipView(epoch=1))
        # même epoch : autorisé (idempotent)
        await store.set_membership(MembershipView(epoch=2))

    async def test_term_monotonic(self, store: HivemindStateStore) -> None:
        await store.bump_term(1, updated_by_node_id="n")
        await store.bump_term(5, updated_by_node_id="n")
        with pytest.raises(RuntimeError):
            await store.bump_term(3, updated_by_node_id="n")
        # idempotent : même term ne déclenche pas de write
        term = await store.bump_term(5, updated_by_node_id="n")
        assert term.term == 5

    async def test_bank_version_pointer_rebuild(self, store: HivemindStateStore) -> None:
        # Trois commits ; le pointeur doit refléter le plus récent.
        for v in range(3):
            await store.append_commit(
                BankCommit(
                    bank_version=v,
                    parent_bank_version=v - 1,
                    term=1,
                    commit_id=f"c{v}",
                    committed_by_node_id="nodeA",
                )
            )
        pointer = await store.rebuild_pointer_from_commits()
        assert pointer is not None
        assert pointer.bank_version == 2
        assert pointer.commit_id == "c2"

    async def test_commit_idempotent_same_commit_id(self, store: HivemindStateStore) -> None:
        c = BankCommit(
            bank_version=0,
            term=1,
            commit_id="c0",
            committed_by_node_id="n",
        )
        first = await store.append_commit(c)
        second = await store.append_commit(c)
        assert first == second

    async def test_commit_conflict_raises(self, store: HivemindStateStore) -> None:
        await store.append_commit(
            BankCommit(bank_version=0, term=1, commit_id="c0", committed_by_node_id="n")
        )
        with pytest.raises(RuntimeError):
            await store.append_commit(
                BankCommit(bank_version=0, term=1, commit_id="OTHER", committed_by_node_id="n")
            )


# =============================================================================
# Déduplication des events (acceptance criterion principal de l'issue)
# =============================================================================


@pytest.mark.asyncio
class TestEventDedup:
    def _envelope(self, event_id: str, ts: str | None = None) -> EventEnvelope:
        return EventEnvelope(
            event_id=event_id,
            type=EventType.TOKEN_CLAIM,
            origin_node_id="nodeA",
            term=1,
            created_at=ts or "2026-05-27T10:00:00+00:00",
        )

    async def test_append_event_first_write_returns_true(self, store: HivemindStateStore) -> None:
        ok = await store.append_event(self._envelope("evt-1"))
        assert ok is True
        assert await store.has_event("evt-1") is True

    async def test_append_event_duplicate_is_noop(
        self, store: HivemindStateStore, storage: FakeStorage
    ) -> None:
        await store.append_event(self._envelope("evt-1"))
        before = storage.snapshot()
        ok = await store.append_event(self._envelope("evt-1"))
        assert ok is False
        assert storage.objects == before, "aucun write attendu sur replay"

    async def test_append_event_duplicate_with_different_content_still_noop(
        self, store: HivemindStateStore
    ) -> None:
        """
        Replay = même event_id, payload potentiellement différent (clock skew,
        retry après crash). On NE met PAS à jour : la première écriture est
        la "vérité" — c'est la sémantique attendue d'un log append-only.
        """
        await store.append_event(self._envelope("evt-1", ts="2026-05-27T10:00:00+00:00"))
        evt2 = EventEnvelope(
            event_id="evt-1",
            type=EventType.TOKEN_RELEASED,  # type différent !
            origin_node_id="nodeA",
            term=99,  # term différent !
            created_at="2026-05-27T11:00:00+00:00",
        )
        ok = await store.append_event(evt2)
        assert ok is False
        # Le journal contient encore l'original (le TOKEN_CLAIM, pas RELEASED).
        events = await store.list_events()
        assert len(events) == 1
        assert events[0].type == EventType.TOKEN_CLAIM.value
        assert events[0].term == 1

    async def test_events_listed_in_chronological_order(self, store: HivemindStateStore) -> None:
        # Insertion désordonnée
        await store.append_event(self._envelope("e3", ts="2026-05-27T10:00:03+00:00"))
        await store.append_event(self._envelope("e1", ts="2026-05-27T10:00:01+00:00"))
        await store.append_event(self._envelope("e2", ts="2026-05-27T10:00:02+00:00"))
        events = await store.list_events()
        assert [e.event_id for e in events] == ["e1", "e2", "e3"]

    async def test_compact_events_before_cutoff(self, store: HivemindStateStore) -> None:
        await store.append_event(self._envelope("e1", ts="2026-05-27T10:00:01+00:00"))
        await store.append_event(self._envelope("e2", ts="2026-05-27T10:00:02+00:00"))
        await store.append_event(self._envelope("e3", ts="2026-05-27T10:00:03+00:00"))
        deleted = await store.compact_events_before("2026-05-27T10:00:02+00:00")
        assert deleted == 1  # seul e1 est antérieur strict
        events = await store.list_events()
        assert [e.event_id for e in events] == ["e2", "e3"]


# =============================================================================
# Queue — FIFO ordering
# =============================================================================


@pytest.mark.asyncio
class TestQueueOrdering:
    async def test_queue_returns_entries_in_sequence_order(
        self, store: HivemindStateStore
    ) -> None:
        # Insertion désordonnée
        for seq in [3, 1, 10, 2]:
            await store.enqueue(
                QueueEntry(
                    event_id=f"evt-{seq}",
                    sequence=seq,
                    requester_node_id="n",
                )
            )
        entries = await store.list_queue()
        assert [e.sequence for e in entries] == [1, 2, 3, 10]

    async def test_queue_status_update_is_idempotent(self, store: HivemindStateStore) -> None:
        entry = await store.enqueue(
            QueueEntry(event_id="e", sequence=1, requester_node_id="n")
        )
        u1 = await store.update_queue_entry_status(entry, QueueEntryStatus.GRANTED)
        u2 = await store.update_queue_entry_status(entry, QueueEntryStatus.GRANTED)
        assert u1.status == u2.status == QueueEntryStatus.GRANTED.value
        all_entries = await store.list_queue()
        assert len(all_entries) == 1

    async def test_remove_queue_entry(self, store: HivemindStateStore) -> None:
        entry = await store.enqueue(
            QueueEntry(event_id="e", sequence=1, requester_node_id="n")
        )
        await store.remove_queue_entry(entry)
        assert await store.list_queue() == []


# =============================================================================
# ACKs — comptage et idempotence par (event_id, node_id)
# =============================================================================


@pytest.mark.asyncio
class TestAcks:
    async def test_record_ack_idempotent_per_event_node(
        self, store: HivemindStateStore, storage: FakeStorage
    ) -> None:
        ack = Ack(event_id="evtA", ack_by_node_id="nodeB", term=1)
        await store.record_ack(ack)
        before_get = storage.get_calls
        await store.record_ack(ack)
        # Une seule clé écrite, ré-écriture autorisée (même contenu)
        keys = [k for k in storage.objects if k.startswith("alpha/_hivemind/acks/evtA/")]
        assert len(keys) == 1
        assert await store.count_acks("evtA") == 1
        # second appel a bien fait des reads (lecture pour vérifier la dédup
        # implicite côté store) — sanity check minimal
        assert storage.get_calls >= before_get

    async def test_multiple_peers_ack_same_event(self, store: HivemindStateStore) -> None:
        for n in ["nodeA", "nodeB", "nodeC"]:
            await store.record_ack(Ack(event_id="evt1", ack_by_node_id=n, term=1))
        acks = await store.list_acks("evt1")
        assert {a.ack_by_node_id for a in acks} == {"nodeA", "nodeB", "nodeC"}
        assert await store.count_acks("evt1") == 3

    async def test_count_acks_does_not_count_corrupted_objects(
        self, store: HivemindStateStore, storage: FakeStorage
    ) -> None:
        """
        Régression P2.3 : un objet corrompu sous ``acks/{event_id}/`` ne doit
        pas être compté vers le quorum. ``count_acks`` doit valider chaque
        record via Pydantic, comme ``list_acks``.
        """
        await store.record_ack(
            Ack(event_id="evt1", ack_by_node_id="nodeA", term=1)
        )
        # Inject un objet corrompu (JSON cassé) au milieu du préfixe ACKs.
        storage.objects["alpha/_hivemind/acks/evt1/nodeBROKEN.json"] = "{not json"

        # Le helper de comptage doit faire la même validation que list_acks
        # et propager la corruption au caller.
        with pytest.raises(CorruptedStateError):
            await store.count_acks("evt1")


# =============================================================================
# Tombstones + watermarks — GC croisée
# =============================================================================


@pytest.mark.asyncio
class TestTombstonesAndWatermarks:
    async def test_garbage_collect_tombstones_with_min_watermark(
        self, store: HivemindStateStore
    ) -> None:
        for note_id, bv in [("n1", 0), ("n2", 1), ("n3", 5)]:
            await store.add_tombstone(
                Tombstone(
                    note_id=note_id,
                    deleted_by_node_id="nodeA",
                    bank_version=bv,
                )
            )
        # Tous les peers ont vu jusqu'à bank_version=2 → on peut GC n1, n2 ;
        # n3 reste (bank_version=5, pas encore atteint).
        deleted = await store.garbage_collect_tombstones(min_bank_version_across_watermarks=2)
        assert deleted == 2
        remaining = [t.note_id for t in await store.list_tombstones()]
        assert remaining == ["n3"]

    async def test_watermark_monotonic(self, store: HivemindStateStore) -> None:
        await store.set_watermark(Watermark(node_id="peerX", bank_version=3))
        with pytest.raises(RuntimeError):
            await store.set_watermark(Watermark(node_id="peerX", bank_version=2))
        # même valeur = idempotent
        await store.set_watermark(Watermark(node_id="peerX", bank_version=3))


# =============================================================================
# Token lease
# =============================================================================


@pytest.mark.asyncio
class TestTokenLease:
    async def test_fencing_token_monotonic(self, store: HivemindStateStore) -> None:
        await store.set_token(
            TokenLeaseState(state=TokenState.HELD, holder_node_id="nodeA", fencing_token=5, term=5)
        )
        # Term + fencing qui descendent ensemble → rejet (P2.2 regression).
        with pytest.raises(RuntimeError):
            await store.set_token(
                TokenLeaseState(state=TokenState.FREE, fencing_token=3, term=3)
            )

    async def test_set_token_rejects_stale_term(self, store: HivemindStateStore) -> None:
        """
        Régression P2.2 : un payload avec un ``term`` antérieur ne doit pas
        pouvoir écraser un état issu d'un term plus récent, même si son
        fencing_token est supérieur. C'était le bug remonté en review.
        """
        await store.set_token(
            TokenLeaseState(
                state=TokenState.HELD,
                holder_node_id="nodeA",
                term=5,
                fencing_token=5,
            )
        )
        # Term descend (5 → 3) — DOIT être rejeté indépendamment du fencing.
        with pytest.raises(RuntimeError, match="Term monotone"):
            await store.set_token(
                TokenLeaseState(
                    state=TokenState.FREE,
                    term=3,
                    fencing_token=5,  # fencing ≥ existant pour isoler le rejet sur term
                )
            )

    async def test_token_state_reload(
        self, store: HivemindStateStore, storage: FakeStorage
    ) -> None:
        lease = TokenLeaseState(
            state=TokenState.HELD,
            holder_node_id="nodeA",
            term=2,
            fencing_token=2,
            granted_at="2026-05-27T10:00:00+00:00",
            lease_until="2026-05-27T10:05:00+00:00",
        )
        await store.set_token(lease)
        # Restart
        store2 = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
        reloaded = await store2.get_token()
        assert reloaded is not None
        assert reloaded.holder_node_id == "nodeA"
        assert reloaded.state == TokenState.HELD.value
        assert reloaded.fencing_token == 2


# =============================================================================
# Corrupted state handling (acceptance criterion)
# =============================================================================


@pytest.mark.asyncio
class TestCorruption:
    async def test_corrupted_json_raises_corrupted_state_error(
        self, store: HivemindStateStore, storage: FakeStorage
    ) -> None:
        # On injecte du JSON cassé directement
        storage.objects[layout.node_key("alpha")] = "{not valid json"
        with pytest.raises(CorruptedStateError) as exc:
            await store.get_node_identity()
        assert "node.json" in str(exc.value)

    async def test_invalid_schema_raises_corrupted_state_error(
        self, store: HivemindStateStore, storage: FakeStorage
    ) -> None:
        # JSON valide mais schéma invalide (champ extra, "extra=forbid")
        storage.objects[layout.term_key("alpha")] = json.dumps(
            {"term": 5, "updated_by_node_id": "n", "unknown_field": True}
        )
        with pytest.raises(CorruptedStateError) as exc:
            await store.get_term()
        assert "TermState" in str(exc.value)

    async def test_corrupted_state_propagates_through_snapshot(
        self, store: HivemindStateStore, storage: FakeStorage
    ) -> None:
        await store.initialize(NodeIdentity(node_id="node1"))
        # Corruption a posteriori d'un fichier
        storage.objects[layout.members_key("alpha")] = "{bad"
        with pytest.raises(CorruptedStateError):
            await store.load_snapshot()


# =============================================================================
# Snapshot — vue agrégée et reload complet
# =============================================================================


@pytest.mark.asyncio
class TestSnapshot:
    async def test_load_snapshot_reflects_full_state(
        self, store: HivemindStateStore
    ) -> None:
        ident = NodeIdentity(node_id="nodeA", display_name="laptop")
        await store.initialize(
            ident,
            initial_members=[Member(node_id="nodeA"), Member(node_id="nodeB")],
        )
        await store.bump_term(3, updated_by_node_id="nodeA")
        await store.set_token(
            TokenLeaseState(
                state=TokenState.HELD,
                holder_node_id="nodeA",
                term=3,
                fencing_token=3,
            )
        )
        await store.append_commit(
            BankCommit(
                bank_version=0,
                term=3,
                commit_id="c0",
                committed_by_node_id="nodeA",
            )
        )
        await store.set_bank_version_pointer(
            BankVersionPointer(bank_version=0, commit_id="c0")
        )
        await store.enqueue(
            QueueEntry(event_id="evtQ", sequence=1, requester_node_id="nodeB")
        )
        await store.add_tombstone(
            Tombstone(note_id="n1", deleted_by_node_id="nodeA", bank_version=0)
        )
        await store.set_watermark(Watermark(node_id="nodeB", bank_version=0))
        await store.append_event(
            EventEnvelope(
                event_id="evt0",
                type=EventType.BANK_COMMITTED,
                origin_node_id="nodeA",
                term=3,
                bank_version=0,
            )
        )

        snap = await store.load_snapshot()
        assert snap.space_id == "alpha"
        assert snap.node and snap.node.node_id == "nodeA"
        assert snap.membership and len(snap.membership.members) == 2
        assert snap.term and snap.term.term == 3
        assert snap.token and snap.token.state == TokenState.HELD.value
        assert snap.bank_version_pointer and snap.bank_version_pointer.bank_version == 0
        assert len(snap.queue) == 1
        assert len(snap.commits) == 1
        assert len(snap.tombstones) == 1
        assert len(snap.watermarks) == 1
        assert snap.known_event_count == 1
