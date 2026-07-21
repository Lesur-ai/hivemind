# -*- coding: utf-8 -*-
"""
Tests pour issue #15 / P5-7 — réplication des short-notes avec provenance.

Déterministe : ``FakeStorage`` (des tests #3) + horloge injectée. Aucun S3 /
réseau réel. Chaque test nomme la mutation qui le rendrait ROUGE.

Couvre :
- couche pure : ``note_id_from_filename`` / ``note_id_from_key`` (garde slash),
  ``provenance_label``, ``advance_event_cursor`` (monotone + port bank_version),
  ``cursor_admits`` (bornes), pin des 15 ``EventType`` ;
- idempotence : même note deux fois -> une copie ;
- curseur d'event : avance monotone, port de ``bank_version``, invisible à la GC ;
- anti-résurrection : tombstone-first, réordre note-first, mismatch d'identité,
  garde writer ;
- provenance : local vs pair, scope Hivemind-only, fail-closed ;
- isolation : scan AST anti graph/long, garde de constructeur, anti chemin token.
"""

from __future__ import annotations

import ast
import inspect
import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from live_mem.core.hivemind import (
    CorruptedStateError,
    EventType,
    HivemindStateStore,
    Tombstone,
    Watermark,
    advance_event_cursor,
    cursor_admits,
    layout,
    note_id_from_filename,
    note_id_from_key,
    origin_note_id,
    provenance_label,
)
from live_mem.core.hivemind import note_replication as note_replication_module
from live_mem.core.hivemind.note_replication import (
    NoteOrigin,
    NoteReplicationConflictError,
    NoteReplicationRuntime,
    ReplicatedNote,
    ReplicationStatus,
)
from live_mem.core.live import LiveService

from tests.test_hivemind_state import FakeStorage


# =============================================================================
# Horloge déterministe + helpers de fabrication
# =============================================================================


class _Clock:
    """Horloge injectable figée (déterminisme des timestamps de sidecar)."""

    def __init__(self, fixed: datetime | None = None) -> None:
        self._fixed = fixed or datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._fixed


SPACE = "alpha"


def _note_md(*, agent: str, category: str, body: str, ts: str, tags: list[str]) -> str:
    """Octets ``.md`` (front-matter + body) au MÊME format que ``live.write_note``."""
    return (
        "---\n"
        f'timestamp: "{ts}"\n'
        f"agent: {json.dumps(agent, ensure_ascii=False)}\n"
        f'category: "{category}"\n'
        f"tags: {json.dumps(tags)}\n"
        f'space_id: "{SPACE}"\n'
        "---\n\n"
    ) + body


def _make_note(
    *,
    stem: str = "20260101T000000_cline_observation_ab12cd34",
    origin_node_id: str = "node-b",
    agent: str = "cline",
    category: str = "observation",
    body: str = "hello from peer",
    ts: str = "2026-01-01T00:00:00+00:00",
    tags: list[str] | None = None,
    filename: str | None = None,
) -> ReplicatedNote:
    tags = tags or ["t1"]
    fn = filename if filename is not None else f"{stem}.md"
    md = _note_md(agent=agent, category=category, body=body, ts=ts, tags=tags)
    return ReplicatedNote(
        note_id=stem,
        filename=fn,
        origin_node_id=origin_node_id,
        origin_agent=agent,
        category=category,
        content=body,
        tags=tags,
        created_at=ts,
        note_md=md,
    )


def _runtime(storage: FakeStorage, *, clock: _Clock | None = None) -> NoteReplicationRuntime:
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    return NoteReplicationRuntime(store, storage, SPACE, clock=clock or _Clock())  # type: ignore[arg-type]


# =============================================================================
# Couche pure
# =============================================================================


def test_P1_note_id_identity_and_slash_guard() -> None:
    """``note_id_from_filename`` strippe un ``.md`` OBLIGATOIRE ; ``note_id_from_key``
    parse ``{space}/live/{stem}.md`` ; tous deux refusent un ``/`` dans l'id. RED
    sans la garde slash."""
    assert note_id_from_filename("foo_bar.md") == "foo_bar"
    assert note_id_from_key("alpha/live/foo_bar.md") == "foo_bar"

    with pytest.raises(ValueError):
        note_id_from_filename("")
    with pytest.raises(ValueError):
        note_id_from_key("no-slash-key")
    # Un id avec slash (clé mal formée déguisant un sous-chemin) est rejeté.
    with pytest.raises(ValueError):
        note_id_from_filename("a/b.md")


def test_P1_note_id_rejects_extensionless_and_foreign_extension() -> None:
    """FAIL-CLOSED : un filename SANS ``.md`` (extensionless ou extension
    étrangère) est REJETÉ par ``note_id_from_filename`` — sans quoi ``"foo"``
    produirait ``note_id "foo"`` sous ``live/foo`` que ``reap_on_tombstone`` (qui
    ne résout que ``{note_id}.md``) laisserait survivre à son tombstone. RED sans
    la garde d'extension."""
    with pytest.raises(ValueError):
        note_id_from_filename("foo")  # extensionless
    with pytest.raises(ValueError):
        note_id_from_filename("foo.txt")  # extension étrangère
    with pytest.raises(ValueError):
        note_id_from_filename("foo.md.bak")  # ne finit pas par .md
    with pytest.raises(ValueError):
        note_id_from_key("alpha/live/foo")  # via la clé S3


def test_P1_origin_note_id_is_alias_not_second_key() -> None:
    """``origin_note_id(note)`` est l'ALIAS documenté == ``note.note_id`` (ADR-0013,
    jamais une 2e clé physique). RED si un champ distinct était introduit."""
    note = _make_note(stem="s1")
    assert origin_note_id(note) == note.note_id == "s1"
    # Le modèle interdit tout champ inconnu (extra="forbid").
    with pytest.raises(Exception):
        ReplicatedNote(
            note_id="s1",
            filename="s1.md",
            origin_node_id="n",
            origin_agent="a",
            note_md="x",
            origin_note_id="s1",  # 2e clé physique interdite
        )


def test_P2_provenance_label_local_vs_peer() -> None:
    """``"<agent> @ local"`` vs ``"<agent> @ <peer_alias>"``. RED sans la branche
    de label."""
    assert (
        provenance_label(origin_agent="cline", is_local=True, peer_alias="node-b")
        == "cline @ local"
    )
    assert (
        provenance_label(origin_agent="cline", is_local=False, peer_alias="node-b")
        == "cline @ node-b"
    )


def test_P3_advance_event_cursor_monotone_and_carries_bank_version() -> None:
    """Curseur monotone sur ``last_event_ts`` (ts plus ancien/égal -> ``prev``
    inchangé, ``is prev``) ET PORT de ``bank_version`` (ADR-0013). RED si le
    curseur hard-code ``bank_version=-1`` ou rewind."""
    prev = Watermark(
        node_id="node-b",
        last_event_id="e1",
        last_event_ts="2026-01-01T00:00:10+00:00",
        bank_version=4,  # progrès appliqué pré-existant
    )
    # ts plus récent -> avance, bank_version PORTÉ inchangé (== 4).
    nxt = advance_event_cursor(
        prev, node_id="node-b", event_id="e2",
        event_ts="2026-01-01T00:00:20+00:00",
    )
    assert nxt is not prev
    assert nxt.last_event_id == "e2"
    assert nxt.last_event_ts == "2026-01-01T00:00:20+00:00"
    assert nxt.bank_version == 4  # JAMAIS réécrit par la réplication

    # ts plus ancien -> pas de rewind, retourne prev tel quel.
    older = advance_event_cursor(
        prev, node_id="node-b", event_id="e0",
        event_ts="2026-01-01T00:00:05+00:00",
    )
    assert older is prev
    # ts égal -> pas d'avance non plus.
    same = advance_event_cursor(
        prev, node_id="node-b", event_id="e1b",
        event_ts="2026-01-01T00:00:10+00:00",
    )
    assert same is prev

    # prev None -> avance, bank_version par défaut -1.
    fresh = advance_event_cursor(
        None, node_id="node-b", event_id="e1",
        event_ts="2026-01-01T00:00:01+00:00",
    )
    assert fresh.bank_version == -1
    assert fresh.last_event_id == "e1"


def test_P3_advance_event_cursor_carries_term_and_epoch_forward_monotone() -> None:
    """MONOTONIE term/epoch (Codex BLOCKING #1) : ``advance_event_cursor`` PORTE
    ``term``/``membership_epoch`` EN AVANT via ``max(existing, incoming)`` — il ne
    peut JAMAIS les faire décroître, même quand l'apply de réplication passe les
    défauts ``0``. RED sans le ``max`` : le curseur écrirait ``term=0``/
    ``membership_epoch=0`` et rembobinerait un watermark posé par le commit_runtime
    (qui porte les vraies valeurs)."""
    prev = Watermark(
        node_id="node-b",
        last_event_id="e1",
        last_event_ts="2026-01-01T00:00:10+00:00",
        bank_version=4,
        term=7,  # term/epoch posés par un commit antérieur
        membership_epoch=3,
    )

    # Event plus récent SANS term/epoch (défauts 0) -> position avance, term/epoch
    # PORTÉS inchangés (max(7,0)=7 ; max(3,0)=3), JAMAIS écrasés à 0.
    nxt = advance_event_cursor(
        prev, node_id="node-b", event_id="e2",
        event_ts="2026-01-01T00:00:20+00:00",
    )
    assert nxt is not prev
    assert nxt.last_event_id == "e2"
    assert nxt.term == 7  # JAMAIS rembobiné à 0
    assert nxt.membership_epoch == 3  # JAMAIS rembobiné à 0
    assert nxt.bank_version == 4

    # Event plus récent AVEC term/epoch supérieurs -> portés en avant (max).
    higher = advance_event_cursor(
        prev, node_id="node-b", event_id="e3",
        event_ts="2026-01-01T00:00:30+00:00",
        term=9, membership_epoch=5,
    )
    assert higher.term == 9
    assert higher.membership_epoch == 5

    # Event plus récent AVEC term/epoch INFÉRIEURs -> jamais de décroissance.
    lower = advance_event_cursor(
        prev, node_id="node-b", event_id="e4",
        event_ts="2026-01-01T00:00:40+00:00",
        term=1, membership_epoch=0,
    )
    assert lower.term == 7  # max(7,1)
    assert lower.membership_epoch == 3  # max(3,0)

    # Event plus ANCIEN avec term/epoch INFÉRIEURs -> rien ne change : prev tel quel.
    older = advance_event_cursor(
        prev, node_id="node-b", event_id="e0",
        event_ts="2026-01-01T00:00:05+00:00",
        term=2, membership_epoch=1,
    )
    assert older is prev  # ni position, ni term/epoch ne progressent


def test_P3_cursor_admits_boundaries() -> None:
    """``cursor_admits`` : ``==`` True, ``<`` False, ``>`` True, ``None`` True.
    RED sans la logique de borne."""
    wm = Watermark(node_id="n", last_event_ts="2026-01-01T00:00:10+00:00")
    assert cursor_admits(None, event_ts="2026-01-01T00:00:00+00:00") is True
    assert cursor_admits(wm, event_ts="2026-01-01T00:00:10+00:00") is True  # ==
    assert cursor_admits(wm, event_ts="2026-01-01T00:00:09+00:00") is False  # <
    assert cursor_admits(wm, event_ts="2026-01-01T00:00:11+00:00") is True  # >


def test_P4_eventtype_frozen_at_16_members() -> None:
    """P5-7 n'ajoute AUCUN membre ``EventType``. P6-1 (issue #87, ADR-0014)
    APPEND ``UNSAFE_RECOVERY_RESTORED`` (append-only safe per le commentaire
    modèle « ces valeurs sont des chaînes persistées dans le journal d'audit,
    elles ne doivent jamais être renommées »). Le jeu des 16 noms est figé.
    RED si un 17e membre est ajouté."""
    assert len(EventType.__members__) == 16
    assert set(EventType.__members__) == {
        "MEMBERSHIP_UPDATED",
        "TERM_BUMPED",
        "TOKEN_CLAIM",
        "TOKEN_GRANTED",
        "TOKEN_RELEASED",
        "TOKEN_ACK",
        "BANK_COMMITTED",
        "TOMBSTONE_RECORDED",
        "WATERMARK_UPDATED",
        "PEER_JOINED",
        "PEER_EVICTED",
        "RESYNC_REQUIRED",
        "RESYNC_COMPLETED",
        "BOOTSTRAP_SNAPSHOT_EXPORTED",
        "BOOTSTRAP_SNAPSHOT_IMPORTED",
        "UNSAFE_RECOVERY_RESTORED",
    }


# =============================================================================
# Idempotence
# =============================================================================


@pytest.mark.asyncio
async def test_N2_replicate_inbound_idempotent_on_event_id() -> None:
    """Même note deux fois -> STORED puis DUPLICATE (``persisted=False``) ;
    EXACTEMENT un objet ``live/{filename}`` byte-identique à ``note_md``. RED sans
    le check first-write-wins (``exists``)."""
    storage = FakeStorage()
    rt = _runtime(storage)
    note = _make_note()

    r1 = await rt.replicate_inbound(note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00")
    assert r1.status == ReplicationStatus.STORED
    assert r1.persisted is True

    # Re-livraison du MÊME event_id (et même note) -> no-op de copie.
    r2 = await rt.replicate_inbound(note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00")
    assert r2.status == ReplicationStatus.DUPLICATE
    assert r2.persisted is False

    key = f"{SPACE}/live/{note.filename}"
    live_objs = [k for k in storage.objects if k.startswith(f"{SPACE}/live/") and k.endswith(".md")]
    assert live_objs == [key]  # exactement une copie
    assert storage.objects[key] == note.note_md  # byte-identique
    # Le sidecar est posé et stable.
    assert await rt.read_origin(note.note_id) is not None


@pytest.mark.asyncio
async def test_N2b_sidecar_carries_event_id() -> None:
    """STORED -> le sidecar de provenance porte le ``event_id`` de la livraison
    (HIVEMIND.md §2 point 7 : la provenance live-note durable porte
    ``origin_node_id``, ``origin_agent``, ``origin_note_id`` ET ``event_id``).
    Comme les octets du ``.md`` sont verbatim, le sidecar est le SEUL endroit
    durable de cette provenance d'event. RED sans la propagation de ``event_id``
    dans ``NoteOrigin`` (le schéma ne le portait pas)."""
    storage = FakeStorage()
    rt = _runtime(storage)
    note = _make_note()

    r = await rt.replicate_inbound(
        note=note, event_id="evt-42", event_ts="2026-01-01T00:00:01+00:00"
    )
    assert r.status == ReplicationStatus.STORED

    origin = await rt.read_origin(note.note_id)
    assert origin is not None
    assert origin.event_id == "evt-42"  # provenance d'event durable
    assert origin.origin_node_id == note.origin_node_id
    assert origin.origin_agent == note.origin_agent


@pytest.mark.asyncio
async def test_N2c_duplicate_repairs_missing_sidecar() -> None:
    """Crash entre ``put(live/)`` et ``_put_origin`` (deux objets S3 distincts, pas
    de transaction) : la copie ``live/{filename}`` existe SANS sidecar — la note
    s'afficherait à tort comme LOCALE. Le retry (DUPLICATE) RÉPARE le sidecar
    manquant avec le ``event_id`` de la livraison, SANS réécrire la copie. RED sans
    la réparation (l'ancien chemin DUPLICATE laissait le sidecar absent)."""
    storage = FakeStorage()
    rt = _runtime(storage)
    note = _make_note()

    # Tentative crashée simulée : la copie .md est posée, AUCUN sidecar.
    key = f"{SPACE}/live/{note.filename}"
    await storage.put(key, note.note_md)
    assert await rt.read_origin(note.note_id) is None  # provenance perdue

    r = await rt.replicate_inbound(
        note=note, event_id="evt-repair", event_ts="2026-01-01T00:00:01+00:00"
    )
    assert r.status == ReplicationStatus.DUPLICATE
    assert r.persisted is False
    assert storage.objects[key] == note.note_md  # copie inchangée, byte-identique

    origin = await rt.read_origin(note.note_id)
    assert origin is not None  # sidecar RÉPARÉ
    assert origin.event_id == "evt-repair"
    assert origin.origin_node_id == note.origin_node_id


@pytest.mark.asyncio
async def test_N2d_duplicate_divergent_bytes_is_conflict() -> None:
    """Même ``note_id`` (identité ADR-0013) mais octets durables DIVERGENTS sous
    ``live/{filename}`` -> ``NoteReplicationConflictError`` (fail-closed) : on ne
    réécrit JAMAIS la copie existante, jamais coalescé en succès silencieux (même
    doctrine que ``QueueReplayConflictError``). RED sans la garde de divergence
    (l'ancien chemin retournait DUPLICATE silencieusement)."""
    storage = FakeStorage()
    rt = _runtime(storage)
    note = _make_note()

    key = f"{SPACE}/live/{note.filename}"
    divergent = note.note_md + "\nTAMPERED"
    await storage.put(key, divergent)  # même filename/note_id, octets différents

    with pytest.raises(NoteReplicationConflictError):
        await rt.replicate_inbound(
            note=note, event_id="evt-x", event_ts="2026-01-01T00:00:01+00:00"
        )
    # La copie existante n'a PAS été réécrite (fail-closed), aucun sidecar posé.
    assert storage.objects[key] == divergent
    assert await rt.read_origin(note.note_id) is None


# =============================================================================
# Curseur de POSITION d'event (jamais bank_version)
# =============================================================================


@pytest.mark.asyncio
async def test_N6_cursor_advances_and_carries_bank_version() -> None:
    """``replicate_inbound`` avance ``last_event_id``/``last_event_ts`` pour
    l'origine ET PORTE ``bank_version`` inchangé d'un watermark pré-semé
    (``bank_version=0``) — ``set_watermark`` ne lève PAS et garde ``0``. Un ts plus
    ancien ne rewind pas. RED si le curseur hard-code ``bank_version=-1`` (la garde
    monotone de ``set_watermark`` lèverait : ``-1 < 0``)."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]

    # Watermark de PROGRÈS APPLIQUÉ pré-existant (comme posé par un commit).
    await store.set_watermark(Watermark(node_id="node-b", bank_version=0))

    note = _make_note(origin_node_id="node-b")
    r = await rt.replicate_inbound(
        note=note, event_id="evt-9", event_ts="2026-01-01T00:00:09+00:00"
    )
    assert r.cursor_advanced is True

    wm = await store.get_watermark("node-b")
    assert wm is not None
    assert wm.last_event_id == "evt-9"
    assert wm.last_event_ts == "2026-01-01T00:00:09+00:00"
    assert wm.bank_version == 0  # PORTÉ inchangé, pas écrasé par -1

    # Un event plus ANCIEN ne rewind pas le curseur.
    r_old = await rt.replicate_inbound(
        note=_make_note(stem="other", origin_node_id="node-b"),
        event_id="evt-1",
        event_ts="2026-01-01T00:00:01+00:00",
    )
    assert r_old.cursor_advanced is False
    wm2 = await store.get_watermark("node-b")
    assert wm2.last_event_id == "evt-9"  # inchangé
    assert wm2.bank_version == 0


@pytest.mark.asyncio
async def test_N6_cursor_does_not_roll_back_term_or_epoch() -> None:
    """Codex BLOCKING #1 (rollback term/epoch) : un watermark posé par le
    commit_runtime porte ``term``/``membership_epoch`` > 0. Un note RÉPLIQUÉ pour
    cette même origine NE DOIT PAS les rembobiner à 0. RED sans le report ``max``
    dans ``advance_event_cursor`` : ``replicate_inbound`` réécrirait ``term=0``/
    ``membership_epoch=0`` (``set_watermark`` ne garde que ``bank_version``)."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]

    # Watermark de PROGRÈS APPLIQUÉ posé par un commit : term/epoch réels.
    await store.set_watermark(
        Watermark(node_id="node-b", bank_version=2, term=7, membership_epoch=3)
    )

    note = _make_note(origin_node_id="node-b")
    r = await rt.replicate_inbound(
        note=note, event_id="evt-9", event_ts="2026-01-01T00:00:09+00:00"
    )
    assert r.cursor_advanced is True

    wm = await store.get_watermark("node-b")
    assert wm is not None
    assert wm.last_event_id == "evt-9"  # la position avance bien
    assert wm.bank_version == 2  # progrès appliqué porté
    assert wm.term == 7  # JAMAIS rembobiné à 0
    assert wm.membership_epoch == 3  # JAMAIS rembobiné à 0


@pytest.mark.asyncio
async def test_N6_stale_prepared_cursor_cannot_roll_back_position() -> None:
    """Codex BLOCKING #2 (write de curseur préparé « stale » sous concurrence) :
    deux applies entrelacés pour la même origine. L'apply A prépare son curseur
    (snapshot pris à l'instant T) PUIS un apply B concurrent avance durablement le
    curseur AVANT que A ne commit. Comme ``replicate_inbound`` RELIT et RE-DÉRIVE
    le watermark au moment d'écrire (``_advance_cursor``), le commit « stale » de A
    NE PEUT PAS rembobiner ``last_event_ts``/``last_event_id``.

    RED sans le fix (l'ancien chemin réutilisait l'objet ``nxt_watermark`` préparé
    et le réécrivait tel quel via ``_commit_cursor``) : le commit stale de A
    rembobinerait le curseur de ``evt-new``/...:20 vers ``evt-old``/...:05."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]

    note_old = _make_note(stem="oldnote", origin_node_id="node-b")

    # On injecte l'entrelacement : APRÈS que l'apply A (de note_old, ts ...:05) ait
    # pris son snapshot fail-closed dans _prepare_cursor, un apply B concurrent
    # avance DURABLEMENT le curseur à ts ...:20. Puis A poursuit jusqu'au commit.
    real_prepare = rt._prepare_cursor

    async def _prepare_then_concurrent_advance(origin_node_id, *, event_id, event_ts):
        prepared = await real_prepare(origin_node_id, event_id=event_id, event_ts=event_ts)
        # Apply B concurrent : avance durablement le curseur PLUS LOIN.
        await store.set_watermark(
            Watermark(
                node_id="node-b",
                last_event_id="evt-new",
                last_event_ts="2026-01-01T00:00:20+00:00",
            )
        )
        return prepared

    with patch.object(rt, "_prepare_cursor", _prepare_then_concurrent_advance):
        r = await rt.replicate_inbound(
            note=note_old, event_id="evt-old", event_ts="2026-01-01T00:00:05+00:00"
        )

    # La note a bien été stockée (copie durable first-write-wins).
    assert r.status == ReplicationStatus.STORED
    # Mais le curseur N'A PAS été rembobiné par le commit stale de A.
    wm = await store.get_watermark("node-b")
    assert wm is not None
    assert wm.last_event_id == "evt-new"  # PAS "evt-old"
    assert wm.last_event_ts == "2026-01-01T00:00:20+00:00"  # PAS ...:05
    # Le commit stale n'a donc pas avancé le curseur (re-lecture -> no-op).
    assert r.cursor_advanced is False


@pytest.mark.asyncio
async def test_N6_gc_ignores_event_cursor() -> None:
    """Un curseur d'event à ts lointain laisse la GC des tombstones intacte (le
    curseur est INVISIBLE à la GC — ADR-0013). RED si la GC consultait le
    curseur."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]

    # Un tombstone associé au commit bank_version=5.
    await store.add_tombstone(
        Tombstone(note_id="dead", deleted_by_node_id="node-a", bank_version=5)
    )
    # Le curseur d'event avance LOIN (ts futur) sans toucher bank_version (-1).
    await rt.replicate_inbound(
        note=_make_note(stem="x", origin_node_id="node-b"),
        event_id="evt-far",
        event_ts="2999-01-01T00:00:00+00:00",
    )
    wm = await store.get_watermark("node-b")
    assert wm.bank_version == -1  # curseur n'a JAMAIS touché bank_version

    # min cross-peer == -1 (à cause du curseur à -1) -> AUCUNE GC (floor -1).
    # On vérifie surtout que la GC ne lit PAS le curseur d'event (ts 2999) comme
    # un progrès : le tombstone survit.
    deleted = await store.garbage_collect_tombstones(min_bank_version_across_watermarks=-1)
    assert deleted == 0
    assert await store.get_tombstone("dead") is not None


@pytest.mark.asyncio
async def test_N_resume_bounds_replay_on_cursor() -> None:
    """``resume_replication`` ne retourne que les candidats AU/APRÈS le curseur ;
    après avancée, re-call retourne ``[]``. RED sans le gating ``cursor_admits``."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]

    n_old = _make_note(stem="old", origin_node_id="node-b")
    n_new = _make_note(stem="new", origin_node_id="node-b")
    n_other = _make_note(stem="z", origin_node_id="node-c")  # autre origine -> exclu
    candidates = [
        (n_old, "2026-01-01T00:00:05+00:00"),
        (n_new, "2026-01-01T00:00:20+00:00"),
        (n_other, "2026-01-01T00:00:30+00:00"),
    ]

    # Pas de curseur -> tout ce qui est de node-b est admis.
    got = await rt.resume_replication(origin_node_id="node-b", candidates=candidates)
    assert {n.note_id for n in got} == {"old", "new"}

    # Avance le curseur au ts du nouveau.
    await store.set_watermark(
        Watermark(node_id="node-b", last_event_id="e", last_event_ts="2026-01-01T00:00:20+00:00")
    )
    got2 = await rt.resume_replication(origin_node_id="node-b", candidates=candidates)
    assert {n.note_id for n in got2} == {"new"}  # old est SOUS le curseur

    # Curseur strictement au-delà de tous -> rien.
    await store.set_watermark(
        Watermark(node_id="node-b", last_event_id="e2", last_event_ts="2026-01-01T00:00:99+00:00")
    )
    assert await rt.resume_replication(origin_node_id="node-b", candidates=candidates) == []


# =============================================================================
# Anti-résurrection (cœur de sûreté)
# =============================================================================


@pytest.mark.asyncio
async def test_N3_tombstone_first_rejects_replication() -> None:
    """Tombstone-first : ``add_tombstone`` PUIS ``replicate_inbound`` ->
    REJECTED_TOMBSTONED, AUCUN ``live/{filename}`` écrit ; le curseur avance quand
    même. RED sans la garde G-tombstone."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]

    note = _make_note(origin_node_id="node-b")
    await store.add_tombstone(
        Tombstone(note_id=note.note_id, deleted_by_node_id="node-a", bank_version=1)
    )

    r = await rt.replicate_inbound(note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00")
    assert r.status == ReplicationStatus.REJECTED_TOMBSTONED
    assert r.persisted is False
    assert await storage.exists(f"{SPACE}/live/{note.filename}") is False
    assert r.cursor_advanced is True  # la position d'event progresse malgré le refus
    wm = await store.get_watermark("node-b")
    assert wm.last_event_id == "evt-1"


@pytest.mark.asyncio
async def test_N4_reorder_note_first_reaped_then_rejected() -> None:
    """Réordre note-first : STORED, puis ``add_tombstone``, puis
    ``reap_on_tombstone`` supprime copie + sidecar ; une re-livraison tardive ->
    REJECTED_TOMBSTONED, copie NON recréée. RED sans reap + G-tombstone."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]
    note = _make_note(origin_node_id="node-b")

    r1 = await rt.replicate_inbound(note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00")
    assert r1.status == ReplicationStatus.STORED
    key = f"{SPACE}/live/{note.filename}"
    assert await storage.exists(key) is True
    assert await rt.read_origin(note.note_id) is not None

    # Le tombstone arrive APRÈS la note (réordre).
    await store.add_tombstone(
        Tombstone(note_id=note.note_id, deleted_by_node_id="node-a", bank_version=2)
    )
    removed = await rt.reap_on_tombstone(note.note_id)
    assert removed is True
    assert await storage.exists(key) is False
    assert await rt.read_origin(note.note_id) is None  # sidecar nettoyé

    # Re-livraison tardive de la même note : la résurrection est REFUSÉE.
    r2 = await rt.replicate_inbound(note=note, event_id="evt-2", event_ts="2026-01-01T00:00:02+00:00")
    assert r2.status == ReplicationStatus.REJECTED_TOMBSTONED
    assert await storage.exists(key) is False  # jamais recréée

    # reap d'un note_id sans copie est un no-op (idempotent).
    assert await rt.reap_on_tombstone("absent-note") is False


@pytest.mark.asyncio
async def test_N5_identity_mismatch_rejected() -> None:
    """``note.note_id != note_id_from_filename(note.filename)`` -> REJECTED_IDENTITY,
    aucun write. RED sans la garde G-identité."""
    storage = FakeStorage()
    rt = _runtime(storage)
    # note_id ne correspond PAS au stem du filename.
    bad = ReplicatedNote(
        note_id="claimed-id",
        filename="actual_stem.md",
        origin_node_id="node-b",
        origin_agent="cline",
        note_md="---\nagent: \"cline\"\n---\n\nbody",
    )
    r = await rt.replicate_inbound(note=bad, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00")
    assert r.status == ReplicationStatus.REJECTED_IDENTITY
    assert r.persisted is False
    assert r.cursor_advanced is False
    assert await storage.exists(f"{SPACE}/live/actual_stem.md") is False


@pytest.mark.asyncio
async def test_N5_extensionless_filename_rejected_on_replication() -> None:
    """FAIL-CLOSED : une note dont le ``filename`` ne finit PAS par ``.md``
    (extensionless ``"foo"``) est REJETÉE à la réplication -> REJECTED_IDENTITY,
    AUCUN objet live/ écrit, curseur NON avancé. Sans la garde d'extension, la
    copie atterrirait sous ``live/foo`` que ``reap_on_tombstone`` ({note_id}.md)
    ne supprimerait jamais — résurrection silencieuse. RED sans la garde
    d'extension dans note_id_from_filename / replicate_inbound."""
    storage = FakeStorage()
    rt = _runtime(storage)
    # note_id == filename (cohérent), mais le filename est extensionless.
    sneaky = ReplicatedNote(
        note_id="foo",
        filename="foo",  # PAS de .md -> rejeté fail-closed
        origin_node_id="node-b",
        origin_agent="cline",
        note_md="---\nagent: \"cline\"\n---\n\nbody",
    )
    r = await rt.replicate_inbound(
        note=sneaky, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00"
    )
    assert r.status == ReplicationStatus.REJECTED_IDENTITY
    assert r.persisted is False
    assert r.cursor_advanced is False
    # AUCUN objet live/ planté (ni live/foo ni live/foo.md).
    assert await storage.exists(f"{SPACE}/live/foo") is False
    assert await storage.exists(f"{SPACE}/live/foo.md") is False
    live_objs = [k for k in storage.objects if k.startswith(f"{SPACE}/live/")]
    assert live_objs == []


@pytest.mark.asyncio
async def test_N_writer_guard_skips_tombstoned_note() -> None:
    """Garde anti-résurrection côté writer, à DEUX niveaux : (1) le caller consulte
    ``get_tombstone`` et skippe en amont ; (2) defense-in-depth, le RUNTIME
    lui-même refuse de construire le payload d'une note tombstonée (``ValueError``)
    même si le caller omet le skip. Une note tombstonée n'est JAMAIS ré-émise. RED
    sans la garde ``get_tombstone`` dans ``build_replicated_note``."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]

    stem = "20260101T000000_cline_observation_ab12cd34"
    filename = f"{stem}.md"
    md = _note_md(agent="cline", category="observation", body="local note", ts="2026-01-01T00:00:00+00:00", tags=["t"])
    await storage.put(f"{SPACE}/live/{filename}", md)

    # Sans tombstone : on construit bien le payload.
    note = await rt.build_replicated_note(filename=filename, local_node_id="node-a")
    assert note.note_id == stem
    assert note.note_md == md  # verbatim
    assert note.origin_node_id == "node-a"

    # Avec tombstone : le caller (modèle writer) skippe.
    await store.add_tombstone(
        Tombstone(note_id=stem, deleted_by_node_id="node-a", bank_version=1)
    )

    async def _writer_fanout_one(fn: str) -> bool:
        """Mime la garde writer : skip si tombstoné, sinon construit/fan-out."""
        nid = note_id_from_filename(fn)
        if await store.get_tombstone(nid) is not None:
            return False
        await rt.build_replicated_note(filename=fn, local_node_id="node-a")
        return True

    assert await _writer_fanout_one(filename) is False  # skippé

    # Defense-in-depth : le RUNTIME refuse lui-même de construire le payload d'une
    # note tombstonée, indépendamment du skip caller ci-dessus. RED sans la garde
    # ``get_tombstone`` dans ``build_replicated_note``.
    with pytest.raises(ValueError, match="anti-résurrection"):
        await rt.build_replicated_note(filename=filename, local_node_id="node-a")


@pytest.mark.asyncio
async def test_build_replicated_note_preserves_inline_delimiter_identity_and_body() -> None:
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    rt = NoteReplicationRuntime(store, storage, SPACE, clock=_Clock())  # type: ignore[arg-type]
    agent = 'a.b---c"quoted'
    body = "REPLICATED BODY --- remains verbatim"
    filename = "20260101T000000_ab---cquoted_decision_ab12cd34.md"
    md = _note_md(
        agent=agent,
        category="decision",
        body=body,
        ts="2026-01-01T00:00:00+00:00",
        tags=["identity"],
    )
    await storage.put(f"{SPACE}/live/{filename}", md)

    note = await rt.build_replicated_note(
        filename=filename,
        local_node_id="node-a",
    )

    assert note.origin_agent == agent
    assert note.category == "decision"
    assert note.tags == ["identity"]
    assert note.content == body
    assert note.note_md == md


# =============================================================================
# Fail-closed (corruption propage)
# =============================================================================


@pytest.mark.asyncio
async def test_N9_corrupt_tombstone_propagates() -> None:
    """Un ``tombstones/{note_id}.json`` corrompu -> ``get_tombstone`` lève
    ``CorruptedStateError`` -> ``replicate_inbound`` propage (aucune copie, aucun
    swallow). RED si enveloppé dans try/except."""
    storage = FakeStorage()
    rt = _runtime(storage)
    note = _make_note()

    # Corrompre la tombstone du note_id avec du JSON cassé.
    await storage.put(layout.tombstone_key(SPACE, note.note_id), "{not json")

    with pytest.raises(CorruptedStateError):
        await rt.replicate_inbound(note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00")
    # Aucune copie écrite malgré l'exception.
    assert await storage.exists(f"{SPACE}/live/{note.filename}") is False


@pytest.mark.asyncio
async def test_N9_corrupt_watermark_propagates() -> None:
    """Symétrique de la corruption tombstone : un ``watermarks/{origin}.json``
    corrompu -> ``get_watermark`` (lu par l'avance du curseur de position) lève
    ``CorruptedStateError`` -> ``replicate_inbound`` propage (aucun swallow). RED
    si la lecture du curseur était enveloppée dans try/except."""
    storage = FakeStorage()
    rt = _runtime(storage)
    note = _make_note()  # origin_node_id == "node-b"

    # Corrompre le watermark de l'origine (lu par le report monotone bank_version).
    await storage.put(layout.watermark_key(SPACE, note.origin_node_id), "{broken")

    with pytest.raises(CorruptedStateError):
        await rt.replicate_inbound(note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00")


@pytest.mark.asyncio
async def test_N9_corrupt_watermark_leaves_no_live_copy() -> None:
    """FAIL-CLOSED SANS MUTATION DURABLE : quand le watermark de l'origine est
    corrompu, ``replicate_inbound`` lève ``CorruptedStateError`` AVANT toute
    écriture durable -> AUCUNE copie ``live/{filename}`` ni sidecar n'est laissée
    visible. RED si la lecture/validation du watermark se faisait APRÈS le
    ``put`` de la copie (l'ordre original : write live/sidecar puis _advance_cursor
    qui lit le watermark)."""
    storage = FakeStorage()
    rt = _runtime(storage)
    note = _make_note()  # origin_node_id == "node-b"

    # Watermark corrompu : sa lecture (désormais AVANT le write) doit lever.
    await storage.put(layout.watermark_key(SPACE, note.origin_node_id), "{broken")

    before = storage.snapshot()
    with pytest.raises(CorruptedStateError):
        await rt.replicate_inbound(
            note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00"
        )
    # La note ne doit PAS être visible (ni la copie, ni le sidecar).
    assert await storage.exists(f"{SPACE}/live/{note.filename}") is False
    assert await storage.exists(layout.origin_key(SPACE, note.note_id)) is False
    # Aucune mutation durable du tout : le storage est byte-identique à l'avant.
    assert storage.objects == before


@pytest.mark.asyncio
async def test_N9_corrupt_sidecar_propagates_on_read_origin() -> None:
    """Un sidecar ``live/_origin/{note_id}.json`` corrompu -> ``read_origin`` lève
    ``CorruptedStateError`` (désérialisation fail-closed). RED si swallow."""
    storage = FakeStorage()
    rt = _runtime(storage)
    await storage.put(layout.origin_key(SPACE, "n1"), "{broken")
    with pytest.raises(CorruptedStateError):
        await rt.read_origin("n1")


# =============================================================================
# Provenance / scope (via LiveService.read_notes)
# =============================================================================


class LiveFakeStorage(FakeStorage):
    """``FakeStorage`` + ``list_and_get`` (ce dont ``read_notes`` a besoin)."""

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        out = []
        for key in sorted(self.objects):
            if not key.startswith(prefix):
                continue
            if exclude_keep and key.endswith(".keep"):
                continue
            out.append({"key": key, "content": self.objects[key]})
        return out


async def _seed_hivemind_space(storage: LiveFakeStorage, *, local_node_id: str = "node-a") -> None:
    """Sème un space Hivemind minimal (node.json + membership ACTIVE) + _meta."""
    from live_mem.core.hivemind import Member, MembershipView, NodeIdentity

    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    await store.set_node_identity(NodeIdentity(node_id=local_node_id, display_name=local_node_id))
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[
                Member(node_id=local_node_id, display_name=local_node_id, public_key="ed25519:x"),
                Member(node_id="node-b", display_name="node-b", public_key="ed25519:y"),
            ],
        )
    )
    await storage.put(f"{SPACE}/_meta.json", "{}")


@pytest.mark.asyncio
async def test_N1_read_notes_provenance_local_vs_peer() -> None:
    """Sur un space Hivemind enrichi : une note d'origine PAIR (sidecar présent,
    ``origin_node_id`` distinct) -> ``is_local=False`` + ``"<agent> @ <peer>"`` ;
    une note LOCALE (pas de sidecar) -> ``is_local=True`` + ``"@ local"``. Le ``.md``
    reste byte-identique à l'origine. RED sans l'enrichissement sidecar."""
    storage = LiveFakeStorage()
    await _seed_hivemind_space(storage, local_node_id="node-a")

    # Note LOCALE (pas de sidecar).
    local_md = _note_md(agent="cline", category="observation", body="local body", ts="2026-01-02T00:00:00+00:00", tags=[])
    await storage.put(f"{SPACE}/live/20260102T000000_cline_observation_local111.md", local_md)

    # Note d'origine PAIR (répliquée via le runtime -> pose le sidecar).
    rt = _runtime(storage)
    peer_note = _make_note(
        stem="20260101T000000_remoteagent_decision_peer2222",
        origin_node_id="node-b",
        agent="remoteagent",
        category="decision",
        body="peer body",
        ts="2026-01-01T00:00:00+00:00",
    )
    await rt.replicate_inbound(note=peer_note, event_id="evt-p", event_ts="2026-01-01T00:00:01+00:00")

    with patch("live_mem.core.live.get_storage", return_value=storage):
        res = await LiveService().read_notes(SPACE, limit=50)
    assert res["status"] == "ok"
    by_id = {n["note_id"]: n for n in res["notes"]}

    peer = by_id["20260101T000000_remoteagent_decision_peer2222"]
    assert peer["provenance"]["is_local"] is False
    assert peer["provenance"]["origin_node_id"] == "node-b"
    assert peer["provenance"]["label"] == "remoteagent @ node-b"
    # Body byte-identique aux octets d'origine (verbatim).
    stored = storage.objects[f"{SPACE}/live/{peer_note.filename}"]
    assert stored == peer_note.note_md

    local = by_id["20260102T000000_cline_observation_local111"]
    assert local["provenance"]["is_local"] is True
    assert local["provenance"]["label"] == "cline @ local"

    # Le sidecar n'est JAMAIS retourné comme une note.
    assert all(not n["filename"].endswith(".json") for n in res["notes"])


@pytest.mark.asyncio
async def test_N7_non_hivemind_space_has_no_provenance_key() -> None:
    """Sur un space NON-Hivemind, ``read_notes`` retourne la forme legacy SANS clé
    ``provenance`` ni ``note_id``, contenu byte-identique. RED si l'enrichissement
    tournait inconditionnellement."""
    storage = LiveFakeStorage()
    await storage.put(f"{SPACE}/_meta.json", "{}")  # space existe mais PAS Hivemind
    md = _note_md(agent="cline", category="observation", body="legacy", ts="2026-01-03T00:00:00+00:00", tags=[])
    await storage.put(f"{SPACE}/live/20260103T000000_cline_observation_leg00001.md", md)

    with patch("live_mem.core.live.get_storage", return_value=storage):
        res = await LiveService().read_notes(SPACE, limit=50)
    assert res["status"] == "ok"
    assert len(res["notes"]) == 1
    note = res["notes"][0]
    assert "provenance" not in note
    assert "note_id" not in note
    assert note["content"] == "legacy"


@pytest.mark.asyncio
async def test_N7b_non_hivemind_read_notes_preserves_origin_prefixed_object() -> None:
    """RÉGRESSION byte-for-byte (Codex BLOCKING) : sur un space NON-Hivemind, un
    objet legacy stocké sous ``live/_origin/...`` est une VRAIE note et doit être
    retourné tel quel. ``read_notes`` ne doit PAS le sauter : le skip du sidecar
    ``_origin/`` n'est légitime QUE sur un space Hivemind confirmé.

    RED sans le fix (skip inconditionnel) : l'objet ``_origin/`` est perdu ->
    ``len(notes) == 1``. GREEN avec le fix (skip gaté Hivemind) : 2 notes,
    contenu byte-identique."""
    storage = LiveFakeStorage()
    await storage.put(f"{SPACE}/_meta.json", "{}")  # space existe mais PAS Hivemind

    # Note legacy "normale".
    md_plain = _note_md(agent="cline", category="observation", body="plain", ts="2026-01-03T00:00:00+00:00", tags=[])
    await storage.put(f"{SPACE}/live/20260103T000000_cline_observation_plain001.md", md_plain)

    # Note legacy stockée FORTUITEMENT sous live/_origin/ (PAS un sidecar P5-7 :
    # ce space n'est pas Hivemind). Doit survivre byte-for-byte.
    md_origin = _note_md(agent="dana", category="decision", body="origin-legacy", ts="2026-01-04T00:00:00+00:00", tags=[])
    origin_key = f"{SPACE}/live/_origin/20260104T000000_dana_decision_org00001.md"
    await storage.put(origin_key, md_origin)

    with patch("live_mem.core.live.get_storage", return_value=storage):
        res = await LiveService().read_notes(SPACE, limit=50)
    assert res["status"] == "ok"
    bodies = sorted(n["content"] for n in res["notes"])
    assert bodies == ["origin-legacy", "plain"]
    # Forme legacy préservée (aucun enrichissement Hivemind).
    for n in res["notes"]:
        assert "provenance" not in n
        assert "note_id" not in n
    # L'objet S3 sous _origin/ n'a PAS été muté (byte-for-byte).
    assert storage.objects[origin_key] == md_origin


@pytest.mark.asyncio
async def test_N7c_non_hivemind_search_notes_preserves_origin_prefixed_object() -> None:
    """RÉGRESSION byte-for-byte (Codex BLOCKING), miroir de N7b côté
    ``search_notes`` : un objet legacy sous ``live/_origin/`` doit matcher la
    recherche sur un space NON-Hivemind. RED sans le fix (skip inconditionnel)."""
    storage = LiveFakeStorage()
    await storage.put(f"{SPACE}/_meta.json", "{}")  # PAS Hivemind

    md_origin = _note_md(agent="dana", category="decision", body="findme-origin", ts="2026-01-04T00:00:00+00:00", tags=[])
    origin_key = f"{SPACE}/live/_origin/20260104T000000_dana_decision_org00002.md"
    await storage.put(origin_key, md_origin)

    with patch("live_mem.core.live.get_storage", return_value=storage):
        res = await LiveService().search_notes(SPACE, query="findme-origin", limit=20)
    assert res["status"] == "ok"
    assert len(res["notes"]) == 1
    assert res["notes"][0]["content"] == "findme-origin"
    assert "provenance" not in res["notes"][0]
    assert storage.objects[origin_key] == md_origin


# =============================================================================
# Isolation (AST + constructeur + anti chemin token)
# =============================================================================


def _imported_module_names(source: str) -> list[str]:
    names: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names += [alias.name for alias in node.names]
    return names


def test_A1_no_graph_or_long_import() -> None:
    """Scan AST des imports de ``note_replication`` : aucun token interdit
    (``graph_push``/``graph_bridge``/``consolidation_queue``/``consolidator``/
    ``long``/``engines.long``). RED si un import graph/long est ajouté."""
    source = inspect.getsource(note_replication_module)
    forbidden = (
        "graph_push",
        "graph_bridge",
        "consolidation_queue",
        "consolidator",
        "long",
        "engines.long",
    )
    for mod in _imported_module_names(source):
        for needle in forbidden:
            assert needle not in mod, (
                f"note_replication importe interdit: {mod!r} (contient {needle!r})"
            )


def test_A3_constructor_rejects_space_mismatch() -> None:
    """``NoteReplicationRuntime(store, storage, 'beta')`` avec
    ``store.space_id=='alpha'`` lève ``ValueError``. RED sans la garde de
    constructeur."""
    storage = FakeStorage()
    store = HivemindStateStore(storage=storage, space_id="alpha")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        NoteReplicationRuntime(store, storage, "beta")  # type: ignore[arg-type]


def test_A_token_no_commit_or_token_path() -> None:
    """Le source de ``note_replication`` ne contient AUCUN
    ``assert_commit_allowed`` / ``set_bank_version_pointer`` / ``append_commit`` /
    ``set_token`` : la réplication n'entre jamais dans le chemin commit/token. RED
    si elle le faisait."""
    source = inspect.getsource(note_replication_module)
    for needle in (
        "assert_commit_allowed",
        "set_bank_version_pointer",
        "append_commit",
        "set_token",
    ):
        assert needle not in source, (
            f"note_replication touche le chemin commit/token: {needle!r}"
        )


# Référence sémantique : NoteOrigin est bien le modèle du sidecar (pin de schéma).
def test_note_origin_schema_is_provenance_only() -> None:
    """Le sidecar ``NoteOrigin`` porte la provenance MANDATÉE par HIVEMIND.md §2
    point 7 — dont ``event_id`` (provenance d'event durable) — et RIEN d'autre (pas
    de body). RED si ``event_id`` n'est pas dans le schéma (le gap protocole que
    figeait l'ancien pin)."""
    o = NoteOrigin(
        note_id="n", origin_node_id="node-b", origin_agent="cline", event_id="evt-1"
    )
    dumped = o.model_dump()
    assert set(dumped) == {
        "protocol_version",
        "note_id",
        "origin_node_id",
        "origin_agent",
        "event_id",
        "created_at",
        "replicated_at",
    }
    assert dumped["event_id"] == "evt-1"
    # ``event_id`` est REQUIS (fail-closed) : l'omettre est rejeté à la validation.
    with pytest.raises(ValidationError):
        NoteOrigin(note_id="n", origin_node_id="node-b", origin_agent="cline")


# =============================================================================
# Consolidation × sidecar de provenance (P5-7 fix — le consolidateur NE doit
# JAMAIS traiter live/_origin/ comme une note ni le supprimer)
# =============================================================================


class _ConsolidatorHiveFakeStorage(LiveFakeStorage):
    """``LiveFakeStorage`` (state-store reads + ``list_and_get``) + ``delete_many``
    — le contrat complet que ``ConsolidatorService.consolidate`` exige
    (``_collect_inputs`` lit via ``list_and_get`` ; la purge des notes consommées
    passe par ``delete_many``)."""

    async def delete_many(self, keys: list[str]) -> int:
        n = 0
        for k in keys:
            if k in self.objects:
                self.objects.pop(k, None)
                n += 1
        return n


class _FrozenDt:
    """Remplace ``consolidator.datetime`` pour figer ``now`` (épilogue meta +
    synthèse). Seul ``now`` est consommé par le chemin d'écriture."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 1, 9, 12, 0, 0, tzinfo=timezone.utc)


def _make_stubbed_consolidator():
    """Construit le VRAI ``ConsolidatorService`` SANS client LLM réel (idiome de
    ``test_proxy``/``test_p3_byte_for_byte_compat``) et stub ``_call_llm`` sur un
    résultat fixe (un seul ``create``)."""
    from unittest.mock import AsyncMock, patch as _patch

    from live_mem.config import Settings
    from live_mem.core.consolidator import ConsolidatorService

    settings = Settings.model_validate(
        {
            "mcp_server_name": "Test",
            "mcp_server_host": "0.0.0.0",
            "mcp_server_port": 8002,
            "mcp_server_debug": False,
            "admin_bootstrap_key": "change_me_in_production",
            "s3_endpoint_url": "",
            "s3_access_key_id": "",
            "s3_secret_access_key": "",
            "s3_bucket_name": "live-mem",
            "s3_region_name": "fr1",
            "llmaas_api_url": "https://api.example.com/v1",
            "llmaas_api_key": "sk-test",
            "llmaas_model": "test-model",
            "llmaas_context_window": 131072,
            "llmaas_max_tokens": 16384,
            "llmaas_temperature": 0.3,
            "default_rules_file": "",
            "consolidation_timeout": 600,
            "consolidation_max_notes": 500,
            "consolidation_batch_size": 5,
            "consolidation_cooldown_seconds": 60,
            "consolidation_validation_enabled": False,
            "compact_threshold": 0.6,
            "bank_file_max_size": 15360,
            "response_max_bytes": 512 * 1024,
            "proxy_url": None,
        }
    )
    with (
        _patch("live_mem.core.consolidator.get_settings", return_value=settings),
        _patch("live_mem.core.consolidator.AsyncOpenAI"),
    ):
        svc = ConsolidatorService()
    svc._call_llm = AsyncMock(
        return_value={
            "status": "ok",
            "data": {
                "file_edits": [
                    {
                        "filename": "activeContext.md",
                        "action": "create",
                        "content": "# Active Context\n\n## Focus\n\n- seeded\n",
                    }
                ],
                "synthesis": "Consolidated the replicated note.",
            },
            "usage": {"total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
        }
    )
    return svc


@pytest.mark.asyncio
async def test_P5_7_consolidation_skips_and_preserves_origin_sidecar() -> None:
    """Sur un space Hivemind CONFIRMÉ, une consolidation qui rencontre un sidecar
    ``live/_origin/{note_id}.json`` NE doit PAS le traiter comme une note ni le
    supprimer : seule la vraie note ``.md`` est consommée, le sidecar SURVIT.

    RED sans le fix : ``_collect_inputs`` ramène le sidecar dans ``notes_raw``
    (donc ``notes_keys``) -> il est compté comme note (``notes_processed == 2``)
    puis SUPPRIMÉ par ``delete_many`` en fin de conso (perte de provenance).
    GREEN avec le fix : le préfixe ``live/_origin/`` est sauté en amont sur un
    space Hivemind confirmé -> ``notes_processed == 1`` et le sidecar persiste."""
    from unittest.mock import patch as _patch

    storage = _ConsolidatorHiveFakeStorage()
    await _seed_hivemind_space(storage, local_node_id="node-a")
    # Rules requises pour que la consolidation collecte des inputs valides.
    await storage.put(f"{SPACE}/_rules.md", "# Rules\n\nBe concise.\n")

    # 1 vraie note live + son sidecar de provenance (P5-7).
    note_key = f"{SPACE}/live/20260109T120000_cline_observation_aaaabbbb.md"
    note_md = _note_md(
        agent="cline",
        category="observation",
        body="real note body",
        ts="2026-01-09T12:00:00+00:00",
        tags=[],
    )
    await storage.put(note_key, note_md)
    sidecar_key = layout.origin_key(SPACE, "20260109T120000_cline_observation_aaaabbbb")
    sidecar_bytes = NoteOrigin(
        note_id="20260109T120000_cline_observation_aaaabbbb",
        origin_node_id="node-b",
        origin_agent="cline",
        event_id="evt-cons-1",
    ).model_dump_json()
    await storage.put(sidecar_key, sidecar_bytes)

    consolidator = _make_stubbed_consolidator()
    with (
        _patch("live_mem.core.consolidator.get_storage", return_value=storage),
        _patch("live_mem.core.consolidator.datetime", _FrozenDt),
    ):
        res = await consolidator.consolidate(SPACE, enforce_cooldown=False)

    assert res.get("status") == "ok", res
    # Seule la vraie note est consommée — le sidecar n'est PAS compté comme note.
    assert res.get("notes_processed") == 1, res
    # La vraie note a bien été supprimée (atomicité conso).
    assert note_key not in storage.objects
    # Le sidecar de provenance SURVIT byte-for-byte (cœur du fix P5-7).
    assert sidecar_key in storage.objects
    assert storage.objects[sidecar_key] == sidecar_bytes


@pytest.mark.asyncio
async def test_P5_7_consolidation_origin_skip_only_on_confirmed_hive() -> None:
    """Garde-fou byte-for-byte : sur un space NON-Hivemind, ``live/_origin/`` n'est
    pas un sidecar P5-7 mais un objet legacy ordinaire — le skip ne s'applique
    PAS. Ici on n'amorce AUCUN état Hivemind ; un objet sous ``live/_origin/`` est
    donc traité comme une note legacy et consommé comme avant P5-7.

    RED si le skip était inconditionnel (gate manquante) : l'objet legacy
    survivrait à tort (``notes_processed == 0``)."""
    from unittest.mock import patch as _patch

    storage = _ConsolidatorHiveFakeStorage()
    # Space NON-Hivemind : pas de node.json / membership ACTIVE, juste _meta.
    await storage.put(f"{SPACE}/_meta.json", "{}")
    await storage.put(f"{SPACE}/_rules.md", "# Rules\n\nBe concise.\n")
    legacy_key = f"{SPACE}/live/_origin/legacy-object.json"
    await storage.put(legacy_key, "legacy content under _origin\n")

    consolidator = _make_stubbed_consolidator()
    with (
        _patch("live_mem.core.consolidator.get_storage", return_value=storage),
        _patch("live_mem.core.consolidator.datetime", _FrozenDt),
    ):
        res = await consolidator.consolidate(SPACE, enforce_cooldown=False)

    assert res.get("status") == "ok", res
    # Non-Hivemind : l'objet legacy sous _origin/ est traité comme une note et
    # consommé (comportement d'avant P5-7 — byte-for-byte préservé).
    assert res.get("notes_processed") == 1, res
    assert legacy_key not in storage.objects
