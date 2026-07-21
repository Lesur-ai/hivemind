# -*- coding: utf-8 -*-
"""
Tests pour issue #14 / P5-6 — staging + commit atomique de bank +
tombstones/watermarks (ADR-0011 / ADR-0007 / ADR-0013 / ADR-0012).

Deux familles :

- **couche pure** : projection meta / manifest / checksums / parenté / exclusion
  graph_memory / min cross-peer / éligibilité GC (U1-U8) ;
- **couche async** ``CommitRuntime`` : stage + apply (la choreographie ordonnée),
  l'autorisation UNIQUE ``assert_commit_allowed`` comme seule porte (A2/A3/A4),
  le fail-closed checksum/stage (A5/A6), exclusion graph_memory (A7), tombstone
  par note_id (A8), deux watermarks indépendants (A9), GC qui attend le peer en
  retard (A10), domination structurelle du gate sur ``append_commit`` (A11), et
  l'injection de crash (C1-C3).

Fakes déterministes (``FakeStorage`` + ``DeterministicClock``), aucun transport.
Chaque test nomme la mutation RED-sans-laquelle il échouerait (pas de test
vacant). L'isolation graph/long est vérifiée par un scan AST (G-AST).
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from live_mem.core.hivemind import (
    BankCommit,
    BankCommitManifestEntry,
    BankVersionPointer,
    CommitApplyError,
    CommitApplyReason,
    CommitDenyReason,
    CommitIntent,
    CommitNotAuthorized,
    CommitRuntime,
    CorruptedStateError,
    HivemindStateStore,
    LeaseRuntime,
    QueueRuntime,
    TermState,
    TokenLeaseState,
    TokenState,
    Tombstone,
    Watermark,
    assert_durable_commit_matches,
    assert_intent_matches_commit,
    assert_no_graph_memory_in_manifest,
    assert_parent_contiguous,
    assert_staging_manifest_matches,
    build_commit_intent,
    build_manifest,
    gc_eligible,
    layout,
    manifest_entry_for,
    min_applied_bank_version,
    staged_meta_text,
    verify_manifest_against_staged,
)
from live_mem.core.hivemind import commit_runtime as commit_runtime_module
from live_mem.core.hivemind.lifecycle import _sha256_bytes
from tests.hivemind_harness import DeterministicClock
from tests.test_hivemind_state import FakeStorage


# =============================================================================
# Helpers partagés
# =============================================================================

SPACE = "alpha"


def make_store(storage: FakeStorage, space_id: str = SPACE) -> HivemindStateStore:
    return HivemindStateStore(storage=storage, space_id=space_id)  # type: ignore[arg-type]


def held_token(
    *,
    holder: str,
    term: int,
    lease_until: str | None,
    epoch: int = 1,
    event_id: str = "evt",
) -> TokenLeaseState:
    return TokenLeaseState(
        state=TokenState.HELD,
        holder_node_id=holder,
        term=term,
        fencing_token=term,
        granted_at="2026-01-01T00:00:00+00:00",
        lease_until=lease_until,
        membership_epoch=epoch,
        event_id=event_id,
    )


def manifest_for(staged: dict[str, str]) -> list[BankCommitManifestEntry]:
    return build_manifest(list(staged.items()))


def make_commit(
    *,
    bank_version: int,
    parent_bank_version: int,
    term: int,
    commit_id: str,
    staged: dict[str, str],
    committed_by: str = "nodeA",
    event_id: str = "evt-c",
    notes_consumed: list[str] | None = None,
    epoch: int = 1,
) -> BankCommit:
    return BankCommit(
        bank_version=bank_version,
        parent_bank_version=parent_bank_version,
        term=term,
        membership_epoch=epoch,
        commit_id=commit_id,
        event_id=event_id,
        committed_by_node_id=committed_by,
        manifest=manifest_for(staged),
        notes_consumed=list(notes_consumed or []),
    )


class FailingStorage(FakeStorage):
    """``FakeStorage`` qui lève sur le Nᵉ ``put`` dont la clé matche un prédicat.

    ``FakeStorage.put_json`` délègue à ``put`` ; on n'intercepte donc QUE ``put``
    (sinon une écriture JSON serait comptée deux fois). Permet d'injecter un crash
    à une frontière précise de l'apply ; ``tripped`` mémorise le déclenchement."""

    def __init__(self, *, fail_on_key_substr: str, nth: int = 1) -> None:
        super().__init__()
        self._substr = fail_on_key_substr
        self._nth = nth
        self._seen = 0
        self.tripped = False

    async def put(self, key, content, content_type="text/plain"):  # type: ignore[override]
        if self._substr in key:
            self._seen += 1
            if self._seen >= self._nth:
                self.tripped = True
                raise RuntimeError(f"injected storage failure on {key!r}")
        await super().put(key, content, content_type)


def _runtime(storage: FakeStorage, clock: DeterministicClock, space_id: str = SPACE):
    """Builder : (store, queue, lease, commit_rt) sur un même storage/clock."""
    store = make_store(storage, space_id)
    queue = QueueRuntime(store, space_id)
    lease = LeaseRuntime(store, space_id, queue, clock=clock.now)
    commit_rt = CommitRuntime(store, storage, space_id, lease, clock=clock.now)  # type: ignore[arg-type]
    return store, queue, lease, commit_rt


async def seed_holder(
    store: HivemindStateStore,
    clock: DeterministicClock,
    *,
    term: int,
    bank_version: int,
    holder: str = "nodeA",
    lease_seconds_ahead: int = 300,
) -> None:
    """Pose term.json, le pointeur, et un token HELD vivant par ``holder``."""
    from datetime import timedelta

    await store.bump_term(term, updated_by_node_id="seed")
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=bank_version, commit_id=f"c{bank_version}")
        if bank_version >= 0
        else BankVersionPointer(bank_version=-1)
    )
    until = (clock.now() + timedelta(seconds=lease_seconds_ahead)).isoformat()
    await store.set_token(held_token(holder=holder, term=term, lease_until=until))


def good_intent_for(commit: BankCommit, *, holder: str = "nodeA") -> CommitIntent:
    return build_commit_intent(commit, holder_node_id=holder, fencing_token=commit.term)


# =============================================================================
# U1-U8 — couche pure
# =============================================================================


def test_manifest_entry_and_build_manifest_deterministic() -> None:
    """U1 — sha256 == ``_sha256_bytes(content.encode())`` (byte-identique au
    checksum de bootstrap) ; manifest trié par path. RED si le checksum diverge
    de l'impl lifecycle ou si l'ordre n'est pas stable."""
    e = manifest_entry_for("activeContext.md", "hello")
    assert e.sha256 == _sha256_bytes(b"hello")
    assert e.size == len(b"hello") == 5

    out = build_manifest([("z.md", "Z"), ("a.md", "A"), ("m.md", "M")])
    assert [m.path for m in out] == ["a.md", "m.md", "z.md"]  # trié
    # déterministe : même entrée -> même sha256/size
    assert build_manifest([("a.md", "A")])[0].sha256 == _sha256_bytes(b"A")


def test_verify_manifest_against_staged_all_branches() -> None:
    """U2 — pass ; corruption -> CHECKSUM_MISMATCH ; manquant ->
    MANIFEST_INCOMPLETE ; extra -> PARTIAL_STAGE. RED si une branche manque."""
    staged = {"a.md": "A", "b.md": "BB"}
    commit = make_commit(
        bank_version=0, parent_bank_version=-1, term=1, commit_id="c0", staged=staged
    )
    # pass
    verify_manifest_against_staged(commit, staged)

    # checksum mismatch (octets divergents pour un path présent)
    with pytest.raises(CommitApplyError) as e1:
        verify_manifest_against_staged(commit, {"a.md": "A", "b.md": "XX"})
    assert e1.value.reason == CommitApplyReason.CHECKSUM_MISMATCH

    # manifest path absent du stagé
    with pytest.raises(CommitApplyError) as e2:
        verify_manifest_against_staged(commit, {"a.md": "A"})
    assert e2.value.reason == CommitApplyReason.MANIFEST_INCOMPLETE

    # path stagé hors manifest
    with pytest.raises(CommitApplyError) as e3:
        verify_manifest_against_staged(
            commit, {"a.md": "A", "b.md": "BB", "c.md": "C"}
        )
    assert e3.value.reason == CommitApplyReason.PARTIAL_STAGE


def test_assert_parent_contiguous() -> None:
    """U3 — (bv=0,parent=-1) OK ; (bv=5,parent=3) -> PARENT_MISMATCH. RED sans
    le contrôle de forme interne du parent."""
    ok = make_commit(
        bank_version=0, parent_bank_version=-1, term=1, commit_id="c0",
        staged={"a.md": "A"},
    )
    assert_parent_contiguous(ok)  # ne lève pas

    bad = BankCommit(
        bank_version=5, parent_bank_version=3, term=1, commit_id="bad",
        committed_by_node_id="nodeA",
    )
    with pytest.raises(CommitApplyError) as e:
        assert_parent_contiguous(bad)
    assert e.value.reason == CommitApplyReason.PARENT_MISMATCH


def test_assert_intent_matches_commit() -> None:
    """U4 — intent.previous != commit.parent -> INTENT_PARENT_MISMATCH. RED sans
    G2 (les deux parents pourraient diverger silencieusement)."""
    commit = make_commit(
        bank_version=2, parent_bank_version=1, term=1, commit_id="c2",
        staged={"a.md": "A"},
    )
    good = CommitIntent(
        holder_node_id="nodeA", term=1, fencing_token=1, bank_version=2,
        previous_bank_version=1, commit_id="c2",
    )
    assert_intent_matches_commit(commit, good)  # ne lève pas

    bad = CommitIntent(
        holder_node_id="nodeA", term=1, fencing_token=1, bank_version=2,
        previous_bank_version=0, commit_id="c2",  # parent du CAS divergent
    )
    with pytest.raises(CommitApplyError) as e:
        assert_intent_matches_commit(commit, bad)
    assert e.value.reason == CommitApplyReason.INTENT_PARENT_MISMATCH

    # version CIBLE du CAS divergente de celle du commit -> aussi fermé. RED sans
    # le check intent.bank_version == commit.bank_version (piège latent : un holder
    # autoriserait un CAS pour la version N pendant que le commit prétend M).
    wrong_target = CommitIntent(
        holder_node_id="nodeA", term=1, fencing_token=1, bank_version=3,
        previous_bank_version=1, commit_id="c2",
    )
    with pytest.raises(CommitApplyError) as e2:
        assert_intent_matches_commit(commit, wrong_target)
    assert e2.value.reason == CommitApplyReason.INTENT_PARENT_MISMATCH

    # G2 lie AUSSI les champs porteurs d'identité/autorisation : un intent valide
    # sur les versions mais divergent sur term/commit_id/holder ne doit JAMAIS
    # autoriser l'apply du commit. RED sans la liaison étendue (le commit non
    # authentifié passerait jusqu'à append_commit).
    stale_term = CommitIntent(
        holder_node_id="nodeA", term=2, fencing_token=2, bank_version=2,
        previous_bank_version=1, commit_id="c2",  # term 2 != commit.term 1
    )
    with pytest.raises(CommitApplyError) as e3:
        assert_intent_matches_commit(commit, stale_term)
    assert e3.value.reason == CommitApplyReason.INTENT_TERM_MISMATCH

    wrong_cid = CommitIntent(
        holder_node_id="nodeA", term=1, fencing_token=1, bank_version=2,
        previous_bank_version=1, commit_id="cEVIL",  # != commit.commit_id c2
    )
    with pytest.raises(CommitApplyError) as e4:
        assert_intent_matches_commit(commit, wrong_cid)
    assert e4.value.reason == CommitApplyReason.INTENT_COMMIT_ID_MISMATCH

    wrong_holder = CommitIntent(
        holder_node_id="nodeEVIL", term=1, fencing_token=1, bank_version=2,
        previous_bank_version=1, commit_id="c2",  # != commit.committed_by nodeA
    )
    with pytest.raises(CommitApplyError) as e5:
        assert_intent_matches_commit(commit, wrong_holder)
    assert e5.value.reason == CommitApplyReason.INTENT_HOLDER_MISMATCH


def test_graph_memory_projection_and_assert() -> None:
    """U5 — ``staged_meta_text`` d'un meta AVEC graph_memory n'a pas la clé ;
    un _meta.json stagé qui la garde -> GRAPH_MEMORY_IN_MANIFEST. RED sans la
    projection / sans le re-check."""
    raw = {
        "space_id": "alpha",
        "consolidation_count": 7,
        "graph_memory": {"endpoint": "https://x", "token": "secret"},
    }
    text = staged_meta_text(raw)
    import json as _json

    projected = _json.loads(text)
    assert "graph_memory" not in projected  # ADR-0012 : structurellement absent
    assert projected["consolidation_count"] == 7  # compteur partagé survit

    # _meta.json projeté passe la garde
    commit_ok = make_commit(
        bank_version=0, parent_bank_version=-1, term=1, commit_id="m0",
        staged={"_meta.json": text},
    )
    assert_no_graph_memory_in_manifest(commit_ok, {"_meta.json": text})

    # _meta.json malformé (graph_memory resté) -> raise
    bad_text = _json.dumps({"space_id": "alpha", "graph_memory": {"a": 1}})
    commit_bad = make_commit(
        bank_version=0, parent_bank_version=-1, term=1, commit_id="m1",
        staged={"_meta.json": bad_text},
    )
    with pytest.raises(CommitApplyError) as e:
        assert_no_graph_memory_in_manifest(commit_bad, {"_meta.json": bad_text})
    assert e.value.reason == CommitApplyReason.GRAPH_MEMORY_IN_MANIFEST


def test_build_commit_intent_sources_parent_from_commit() -> None:
    """U6 — previous_bank_version == commit.parent_bank_version (PAS bank_version-1
    re-dérivé). term/fencing câblés. RED si la source du parent change."""
    # commit avec un parent volontairement NON contigu pour prouver que l'intent
    # copie le parent DU COMMIT, pas une re-dérivation bank_version-1.
    commit = BankCommit(
        bank_version=5, parent_bank_version=3, term=4, commit_id="c5",
        committed_by_node_id="nodeA",
    )
    intent = build_commit_intent(commit, holder_node_id="nodeA", fencing_token=4)
    assert intent.previous_bank_version == 3  # == commit.parent, pas 4
    assert intent.bank_version == 5
    assert intent.term == 4 and intent.fencing_token == 4
    assert intent.commit_id == "c5"
    assert intent.holder_node_id == "nodeA"


def test_min_applied_bank_version() -> None:
    """U7 — vide -> -1 ; un peer à -1 -> -1 ; [2,3,2] -> 2. RED sans min/empty."""
    assert min_applied_bank_version([]) == -1
    wms = [
        Watermark(node_id="a", bank_version=2),
        Watermark(node_id="b", bank_version=-1),
    ]
    assert min_applied_bank_version(wms) == -1
    wms2 = [
        Watermark(node_id="a", bank_version=2),
        Watermark(node_id="b", bank_version=3),
        Watermark(node_id="c", bank_version=2),
    ]
    assert min_applied_bank_version(wms2) == 2


def test_gc_eligible_strict() -> None:
    """U8 — bv=-1 -> False ; bv=2,min=2 -> False (strict) ; bv=2,min=3 -> True.
    Mirror de state.garbage_collect_tombstones."""
    t_unassoc = Tombstone(note_id="n", deleted_by_node_id="x", bank_version=-1)
    assert gc_eligible(t_unassoc, 5) is False
    t2 = Tombstone(note_id="n", deleted_by_node_id="x", bank_version=2)
    assert gc_eligible(t2, 2) is False  # strict
    assert gc_eligible(t2, 3) is True


# =============================================================================
# A1-A11 — apply / autorisation / fail-closed
# =============================================================================


async def _stage_and_commit(
    commit_rt: CommitRuntime,
    *,
    bank_version: int,
    parent_bank_version: int,
    term: int,
    commit_id: str,
    staged: dict[str, str],
    committed_by: str = "nodeA",
    event_id: str,
    notes_consumed: list[str] | None = None,
) -> BankCommit:
    return await commit_rt.stage_commit(
        commit_id=commit_id,
        proposed_bank=list(staged.items()),
        bank_version=bank_version,
        parent_bank_version=parent_bank_version,
        term=term,
        membership_epoch=1,
        committed_by_node_id=committed_by,
        event_id=event_id,
        notes_consumed=list(notes_consumed or []),
    )


async def test_apply_happy_path_advances_and_emits() -> None:
    """A1 — stage->apply avance le pointeur -1->0 puis 0->1 ; journal des deux ;
    tombstones avec bank_version du commit ; watermark.bank_version==1 et curseur
    d'event vide ; 3 events ; token FREE après apply du holder ; re-apply no-op.
    RED sans n'importe quelle étape d'apply."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    # --- commit 0 : parent -1 ---
    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cid0",
        staged={"activeContext.md": "v0"}, event_id="evt0",
        notes_consumed=["note-1"],
    )
    intent0 = good_intent_for(c0)
    ptr0 = await commit_rt.apply_commit(
        c0, intent0, local_node_id="nodeA", fencing_token=2
    )
    assert ptr0.bank_version == 0
    # bank vivant promu
    assert storage.objects[f"{SPACE}/bank/activeContext.md"] == "v0"
    # journal
    assert (await store.get_commit(0)).commit_id == "cid0"
    # tombstone avec bank_version du commit consommant
    tomb = await store.get_tombstone("note-1")
    assert tomb is not None and tomb.bank_version == 0
    # watermark : bank_version appliqué, curseur d'event VIDE
    wm = await store.get_watermark("nodeA")
    assert wm is not None and wm.bank_version == 0
    assert wm.last_event_id == "" and wm.last_event_ts == ""
    # events d'audit — id de dédup synthétique dérivé du commit_id (toujours
    # présent/unique), PAS du commit.event_id (qui peut défauter "").
    assert await store.has_event("cid0:committed")
    assert await store.has_event("cid0:tombstone")
    assert await store.has_event("cid0:watermark:nodeA")
    # token libéré (holder a release en étape 9)
    tok = await store.get_token()
    assert tok is not None and tok.state == TokenState.FREE.value

    # idempotence du JOURNAL : ré-écrire le même commit_id à la même version est un
    # no-op (append_commit idempotent), le pointeur reste à 0.
    await store.append_commit(c0)
    assert (await store.get_bank_version_pointer()).bank_version == 0
    assert (await store.get_commit(0)).commit_id == "cid0"

    # --- commit 1 : parent 0 (chaîne contiguë) ---
    # ré-acquérir un token HELD vivant au term courant pour repasser G0 (le holder
    # a release après c0) ; term.json reste 2, pointeur 0.
    from datetime import timedelta

    await store.set_token(
        held_token(
            holder="nodeA", term=2,
            lease_until=(clock.now() + timedelta(seconds=300)).isoformat(),
        )
    )
    c1 = await _stage_and_commit(
        commit_rt, bank_version=1, parent_bank_version=0, term=2, commit_id="cid1",
        staged={"activeContext.md": "v1"}, event_id="evt1", notes_consumed=[],
    )
    intent1 = good_intent_for(c1)
    ptr1 = await commit_rt.apply_commit(
        c1, intent1, local_node_id="nodeA", fencing_token=2
    )
    assert ptr1.bank_version == 1
    assert (await store.get_commit(1)).commit_id == "cid1"
    assert storage.objects[f"{SPACE}/bank/activeContext.md"] == "v1"
    wm1 = await store.get_watermark("nodeA")
    assert wm1.bank_version == 1
    # notes_consumed vide -> pas d'event tombstone pour evt1
    assert not await store.has_event("evt1:tombstone")


async def test_apply_gate_before_append_closes_fencing_hole() -> None:
    """A2 — un intent stale-term fait lever CommitNotAuthorized(STALE_TERM) ET
    aucun commits/{N} n'est écrit ET le pointeur est inchangé. RED si G0 tournait
    APRÈS append_commit."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    # holder HELD au term 2, mais term.json bumpé à 5 -> intent term-2 est stale.
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")
    await store.bump_term(5, updated_by_node_id="nodeB")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cid0",
        staged={"a.md": "A"}, event_id="evt0",
    )
    intent = good_intent_for(c0)  # term 2, désormais stale
    with pytest.raises(CommitNotAuthorized) as e:
        await commit_rt.apply_commit(c0, intent, local_node_id="nodeA", fencing_token=2)
    assert e.value.reason == CommitDenyReason.STALE_TERM
    # AUCUN commit matérialisé, pointeur inchangé.
    assert await store.get_commit(0) is None
    assert layout.commit_key(SPACE, 0) not in storage.objects
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert f"{SPACE}/bank/a.md" not in storage.objects


async def test_apply_g2_binds_intent_identity_to_commit_fail_before_append() -> None:
    """A2bis — G0 autorise un intent VALIDE (term/holder/version courants), mais le
    ``BankCommit`` STAGÉ réellement appliqué diverge sur term / commit_id /
    committed_by. G2 doit FERMER chaque cas AVANT ``append_commit`` : aucun
    commits/0, pointeur -1, bank vivant intouché. RED sans la liaison G2 étendue (le
    commit non authentifié serait journalisé + matérialisé depuis ses propres
    octets)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    # term.json=2, pointeur -1, token HELD vivant par nodeA au term 2.
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    # Trois commits RÉELLEMENT STAGÉS (MANIFEST publié, donc G3 passerait SANS le fix
    # -> RED fidèle) à bank_version 0/parent -1 ; chacun diverge d'UN champ d'identité
    # de l'intent autorisé. Seul G2 doit les arrêter.
    forged_term = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=1, commit_id="cidT",
        staged={"a.md": "A"}, committed_by="nodeA", event_id="evtT",
    )
    forged_cid = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidC",
        staged={"a.md": "A"}, committed_by="nodeA", event_id="evtC",
    )
    forged_holder = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidH",
        staged={"a.md": "A"}, committed_by="nodeEVIL", event_id="evtH",
    )

    # (1) intent VALIDE (term 2 courant) pointant cidT dont le term=1 est stale.
    intent_term = CommitIntent(
        holder_node_id="nodeA", term=2, fencing_token=2, bank_version=0,
        previous_bank_version=-1, commit_id="cidT",
    )
    with pytest.raises(CommitApplyError) as e_term:
        await commit_rt.apply_commit(
            forged_term, intent_term, local_node_id="nodeA", fencing_token=2
        )
    assert e_term.value.reason == CommitApplyReason.INTENT_TERM_MISMATCH

    # (2) intent VALIDE mais commit_id divergent du commit réellement appliqué.
    intent_cid = CommitIntent(
        holder_node_id="nodeA", term=2, fencing_token=2, bank_version=0,
        previous_bank_version=-1, commit_id="cidEVIL",
    )
    with pytest.raises(CommitApplyError) as e_cid:
        await commit_rt.apply_commit(
            forged_cid, intent_cid, local_node_id="nodeA", fencing_token=2
        )
    assert e_cid.value.reason == CommitApplyReason.INTENT_COMMIT_ID_MISMATCH

    # (3) intent VALIDE (holder nodeA, autorisé par le token) mais committer mal
    #     attribué (nodeEVIL).
    intent_holder = CommitIntent(
        holder_node_id="nodeA", term=2, fencing_token=2, bank_version=0,
        previous_bank_version=-1, commit_id="cidH",
    )
    with pytest.raises(CommitApplyError) as e_holder:
        await commit_rt.apply_commit(
            forged_holder, intent_holder, local_node_id="nodeA", fencing_token=2
        )
    assert e_holder.value.reason == CommitApplyReason.INTENT_HOLDER_MISMATCH

    # Aucun des trois n'a été matérialisé : pas de journal à 0, pointeur encore -1,
    # bank vivant intouché.
    assert await store.get_commit(0) is None
    assert layout.commit_key(SPACE, 0) not in storage.objects
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert f"{SPACE}/bank/a.md" not in storage.objects


async def test_apply_rollforward_absent_pointer_fails_closed() -> None:
    """A-roll — un record durable ``commits/0`` présent AVEC un ``bank_version.json``
    ABSENT n'est PAS un pointeur « en retard » : c'est un état critique incomplet.
    ``apply_commit`` doit FERMER en ``CorruptedStateError`` (recovery explicite
    requise), sans matérialiser le bank vivant ni CRÉER de pointeur. RED sans le fix :
    le roll-forward no-lease matérialiserait + créerait le pointeur — fail-OPEN, sans
    aucune autorisation lease/term."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)

    # On STAGE (MANIFEST publié) puis on écrit SEULEMENT le journal durable commits/0
    # (journal-first), SANS pointeur ni bank vivant : exactement l'état "durable +
    # stage présents, pointeur absent" que le fix doit fermer.
    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cid0",
        staged={"a.md": "A"}, committed_by="nodeA", event_id="evt0",
    )
    await store.append_commit(c0)
    assert await store.get_commit(0) is not None  # journal durable présent
    assert await store.get_bank_version_pointer() is None  # pointeur ABSENT

    intent = good_intent_for(c0)
    with pytest.raises(CorruptedStateError):
        await commit_rt.apply_commit(c0, intent, local_node_id="nodeA", fencing_token=2)

    # Fail-closed : aucune mutation. Pointeur PAS créé, bank vivant intouché. (Le
    # journal commits/0 préexistant reste tel quel : la reprise est interdite, pas le
    # journal.)
    assert await store.get_bank_version_pointer() is None
    assert layout.bank_version_key(SPACE) not in storage.objects
    assert f"{SPACE}/bank/a.md" not in storage.objects


async def test_apply_stale_previous_bank_version_rejected_pointer_unchanged() -> None:
    """A3 — intent.previous != pointeur vivant -> VERSION_CONFLICT ; pointeur
    byte-identique avant/après, pas de journal/tombstone/watermark. RED sans G0."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    # pointeur vivant à 3, holder HELD au term 2.
    await seed_holder(store, clock, term=2, bank_version=3, holder="nodeA")

    # commit qui prétend parent 3 -> intent.previous == 3 ; mais on falsifie un
    # intent stale (previous=0) tout en gardant un commit cohérent parent=3.
    c4 = await _stage_and_commit(
        commit_rt, bank_version=4, parent_bank_version=3, term=2, commit_id="cid4",
        staged={"a.md": "A"}, event_id="evt4",
    )
    stale_intent = CommitIntent(
        holder_node_id="nodeA", term=2, fencing_token=2, bank_version=4,
        previous_bank_version=0,  # vivant=3 -> conflict
        commit_id="cid4",
    )
    before = storage.snapshot()
    with pytest.raises(CommitNotAuthorized) as e:
        await commit_rt.apply_commit(c4, stale_intent, local_node_id="nodeA", fencing_token=2)
    assert e.value.reason == CommitDenyReason.VERSION_CONFLICT
    # le pointeur (et tout le reste) est inchangé byte-pour-byte.
    assert storage.objects[layout.bank_version_key(SPACE)] == before[
        layout.bank_version_key(SPACE)
    ]
    assert await store.get_commit(4) is None
    assert await store.get_watermark("nodeA") is None


async def test_apply_wrong_fencing_and_expired_rejected() -> None:
    """A4 — fencing != term courant -> STALE_TERM ; holder expiré non-superseded
    -> FENCED ; zéro mutation dans les deux cas."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cid0",
        staged={"a.md": "A"}, event_id="evt0",
    )
    # fencing divergent : intent.fencing_token=9 casse la chaîne d'égalité.
    bad_fencing = CommitIntent(
        holder_node_id="nodeA", term=2, fencing_token=9, bank_version=0,
        previous_bank_version=-1, commit_id="cid0",
    )
    with pytest.raises(CommitNotAuthorized) as e1:
        await commit_rt.apply_commit(c0, bad_fencing, local_node_id="nodeA", fencing_token=9)
    assert e1.value.reason == CommitDenyReason.STALE_TERM
    assert await store.get_commit(0) is None

    # holder expiré (lease élapsée), term inchangé -> FENCED.
    clock.tick(seconds=301)
    good = good_intent_for(c0)
    with pytest.raises(CommitNotAuthorized) as e2:
        await commit_rt.apply_commit(c0, good, local_node_id="nodeA", fencing_token=2)
    assert e2.value.reason == CommitDenyReason.FENCED
    assert await store.get_commit(0) is None


async def test_apply_checksum_mismatch_fails_closed() -> None:
    """A5 — un fichier stagé corrompu post-stage -> CommitApplyError(CHECKSUM) ;
    bank vivant, commits/, pointeur, tombstones, watermark TOUS inchangés. RED si
    G3 tournait après l'étape 1 (promote)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cid0",
        staged={"a.md": "A", "b.md": "BB"}, event_id="evt0", notes_consumed=["n1"],
    )
    # corrompre les octets stagés d'un fichier APRÈS le stage (le sha256 du
    # manifest ne matchera plus).
    storage.objects[layout.staging_bank_key(SPACE, "cid0", "b.md")] = "XX"

    before = storage.snapshot()
    with pytest.raises(CommitApplyError) as e:
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert e.value.reason == CommitApplyReason.CHECKSUM_MISMATCH
    # zéro apply : bank vivant absent, pas de commit, pointeur -1, pas de tombstone.
    assert f"{SPACE}/bank/a.md" not in storage.objects
    assert f"{SPACE}/bank/b.md" not in storage.objects
    assert await store.get_commit(0) is None
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert await store.get_tombstone("n1") is None
    assert await store.get_watermark("nodeA") is None
    assert storage.snapshot() == before  # rien n'a bougé


async def test_apply_partial_stage_and_incomplete_manifest_fail_closed() -> None:
    """A6 — manifest liste 2 paths, 1 seul stagé -> MANIFEST_INCOMPLETE ; un objet
    stagé hors manifest -> PARTIAL_STAGE ; zéro mutation."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    # INCOMPLETE : on stage 2 fichiers puis on en SUPPRIME un du staging.
    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidI",
        staged={"a.md": "A", "b.md": "B"}, event_id="evtI",
    )
    del storage.objects[layout.staging_bank_key(SPACE, "cidI", "b.md")]
    with pytest.raises(CommitApplyError) as e1:
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert e1.value.reason == CommitApplyReason.MANIFEST_INCOMPLETE
    assert await store.get_commit(0) is None

    # PARTIAL_STAGE : manifest d'1 path mais 2 fichiers présents dans load_staged.
    # On construit un commit dont le manifest ne couvre QUE a.md, mais on a stagé
    # a.md ET b.md sous le même commit_id. load_staged ne lit que le manifest, donc
    # on teste la garde pure directement avec un staged dict élargi.
    c1 = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="cidP",
        staged={"a.md": "A"},  # manifest = {a.md}
    )
    with pytest.raises(CommitApplyError) as e2:
        verify_manifest_against_staged(c1, {"a.md": "A", "b.md": "B"})
    assert e2.value.reason == CommitApplyReason.PARTIAL_STAGE


async def test_apply_graph_memory_excluded_end_to_end() -> None:
    """A7 — _meta.json stagé via staged_meta_text ne contient pas graph_memory,
    un compteur partagé survit, et la garde passe ; un commit hand-built avec
    graph_memory résiduel -> GRAPH_MEMORY_IN_MANIFEST. RED sans la projection /
    le re-check (ADR-0012)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    raw_meta = {
        "space_id": "alpha",
        "consolidation_count": 11,
        "graph_memory": {"endpoint": "https://x", "token": "secret", "memory_id": "m"},
    }
    meta_text = staged_meta_text(raw_meta)
    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidM",
        staged={"_meta.json": meta_text}, event_id="evtM",
    )
    # aucune entrée de manifest ne contient "graph_memory"
    staged = await commit_rt.load_staged("cidM", c0.manifest)
    assert all("graph_memory" not in txt for txt in staged.values())
    assert '"consolidation_count":11' in staged["_meta.json"]  # compteur survit

    ptr = await commit_rt.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    assert ptr.bank_version == 0
    live_meta = storage.objects[f"{SPACE}/bank/_meta.json"]
    assert "graph_memory" not in live_meta

    # commit hand-built dont le _meta.json stagé garde graph_memory -> raise.
    import json as _json

    bad_text = _json.dumps({"space_id": "alpha", "graph_memory": {"a": 1}})
    bad = make_commit(
        bank_version=1, parent_bank_version=0, term=2, commit_id="cidBad",
        staged={"_meta.json": bad_text},
    )
    with pytest.raises(CommitApplyError) as e:
        assert_no_graph_memory_in_manifest(bad, {"_meta.json": bad_text})
    assert e.value.reason == CommitApplyReason.GRAPH_MEMORY_IN_MANIFEST


async def test_tombstone_keyed_on_note_id_single_object_idempotent() -> None:
    """A8 — apply consommant la note N : get_tombstone(N).bank_version ==
    commit.bank_version ; exactement 1 objet sous tombstone_key ; re-apply ->
    toujours 1. RED si un second origin_note_id était écrit."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidT",
        staged={"a.md": "A"}, event_id="evtT", notes_consumed=["N"],
    )
    await commit_rt.apply_commit(c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2)
    tomb = await store.get_tombstone("N")
    assert tomb is not None and tomb.bank_version == 0
    assert tomb.deleted_by_node_id == "nodeA"
    # exactement UN objet sous le préfixe tombstones (pas de second key).
    tomb_objs = [k for k in storage.objects if k.startswith(layout.tombstone_prefix(SPACE))]
    assert tomb_objs == [layout.tombstone_key(SPACE, "N")]

    # re-add idempotent (même clé) : toujours un seul objet.
    await store.add_tombstone(tomb)
    tomb_objs2 = [k for k in storage.objects if k.startswith(layout.tombstone_prefix(SPACE))]
    assert tomb_objs2 == [layout.tombstone_key(SPACE, "N")]


async def test_two_watermarks_never_substitute() -> None:
    """A9 — après apply : watermark.bank_version==N et last_event_id/ts == leurs
    valeurs pré-apply (portées, pas écrasées) ; un watermark à bank_version=-1
    avec un last_event_ts énorme laisse gc_tombstones à 0 (GC invariante au
    curseur). RED si l'étape 6 écrivait le curseur d'event ou si la GC le lisait."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    # pré-poser un watermark avec un curseur d'event NON-VIDE pour nodeA.
    await store.set_watermark(
        Watermark(
            node_id="nodeA", bank_version=-1,
            last_event_id="evt-prev", last_event_ts="2099-12-31T00:00:00+00:00",
        )
    )
    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidW",
        staged={"a.md": "A"}, event_id="evtW", notes_consumed=["n1"],
    )
    await commit_rt.apply_commit(c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2)
    wm = await store.get_watermark("nodeA")
    assert wm.bank_version == 0  # progrès appliqué avancé
    # curseur d'event PORTÉ inchangé (jamais écrit par l'apply).
    assert wm.last_event_id == "evt-prev"
    assert wm.last_event_ts == "2099-12-31T00:00:00+00:00"

    # GC invariante au curseur : ce nodeA est le seul peer attendu, bank_version=0
    # < tombstone.bank_version=0 ? non, strict -> rien évincé ; mais surtout
    # un curseur énorme ne doit pas débloquer la GC.
    deleted = await commit_rt.gc_tombstones(expected_node_ids={"nodeA"})
    assert deleted == 0
    assert await store.get_tombstone("n1") is not None  # retenu


async def test_gc_waits_for_lagging_peer() -> None:
    """A10 — 3 nodes, tombstone à bank_version=V. A=V,B=V,C=V-1 -> 0 (retenu) ;
    C absent -> 0 ; tous à V+1 -> évincé. RED sans le min/expected-set gate."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)

    V = 3
    await store.add_tombstone(
        Tombstone(note_id="n", deleted_by_node_id="nodeA", bank_version=V)
    )
    # A=V, B=V, C=V-1
    await store.set_watermark(Watermark(node_id="A", bank_version=V))
    await store.set_watermark(Watermark(node_id="B", bank_version=V))
    await store.set_watermark(Watermark(node_id="C", bank_version=V - 1))
    assert await commit_rt.gc_tombstones(expected_node_ids={"A", "B", "C"}) == 0
    assert await store.get_tombstone("n") is not None

    # C absent entièrement (on attend C mais il n'a pas de watermark) -> 0.
    storage2 = FakeStorage()
    store2, q2, l2, rt2 = _runtime(storage2, clock)
    await store2.add_tombstone(
        Tombstone(note_id="n", deleted_by_node_id="nodeA", bank_version=V)
    )
    await store2.set_watermark(Watermark(node_id="A", bank_version=V + 5))
    await store2.set_watermark(Watermark(node_id="B", bank_version=V + 5))
    assert await rt2.gc_tombstones(expected_node_ids={"A", "B", "C"}) == 0
    assert await store2.get_tombstone("n") is not None

    # tous à V+1 (min strict > V) -> évincé.
    await store.set_watermark(Watermark(node_id="C", bank_version=V + 1))
    await store.set_watermark(Watermark(node_id="A", bank_version=V + 1))
    await store.set_watermark(Watermark(node_id="B", bank_version=V + 1))
    assert await commit_rt.gc_tombstones(expected_node_ids={"A", "B", "C"}) == 1
    assert await store.get_tombstone("n") is None


def test_structural_gate_dominates_append_commit() -> None:
    """A11 — invariant de SINGLE point d'autorisation + gate-avant-journal sur le
    chemin FRAIS, robuste au refactor (apply_commit délègue la matérialisation à
    _materialize_commit).

    On scanne l'AST des DEUX méthodes :

    - ``assert_commit_allowed`` apparaît EXACTEMENT une fois, et SEULEMENT dans
      ``apply_commit`` (le point d'autorisation UNIQUE — ``_materialize_commit`` n'en
      contient AUCUN, car la reprise roll-forward s'exécute volontairement SANS
      lease) ;
    - ``append_commit`` apparaît EXACTEMENT une fois, et SEULEMENT dans
      ``_materialize_commit`` (le journal-first, appelé par le chemin frais APRÈS le
      gate, et par la reprise roll-forward sur un commit déjà autorisé) ;
    - sur le CHEMIN FRAIS, le gate précède lexicalement l'appel à
      ``_materialize_commit`` (qui porte l'``append_commit``).

    RED si : un second ``assert_commit_allowed`` apparaît (auth dupliquée) ; si
    ``_materialize_commit`` ré-introduit une autorisation (la reprise re-exigerait un
    lease) ; si ``append_commit`` est dupliqué/hors-gate ; ou si le gate ne précède
    plus la matérialisation sur le chemin frais."""
    apply_src = textwrap.dedent(
        inspect.getsource(commit_runtime_module.CommitRuntime.apply_commit)
    )
    mat_src = textwrap.dedent(
        inspect.getsource(commit_runtime_module.CommitRuntime._materialize_commit)
    )

    def _attr_lines(source: str, attr: str) -> list[int]:
        tree = ast.parse(source)
        return [
            n.lineno
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == attr
        ]

    # autorisation : exactement 1, et UNIQUEMENT dans apply_commit.
    auth_in_apply = _attr_lines(apply_src, "assert_commit_allowed")
    auth_in_mat = _attr_lines(mat_src, "assert_commit_allowed")
    assert len(auth_in_apply) == 1, (
        f"assert_commit_allowed attendu 1 fois dans apply_commit, vu {auth_in_apply}"
    )
    assert auth_in_mat == [], (
        "assert_commit_allowed ne doit PAS apparaître dans _materialize_commit "
        "(la reprise roll-forward s'exécute SANS lease — point d'auth unique)"
    )

    # journal : exactement 1, et UNIQUEMENT dans _materialize_commit.
    append_in_apply = _attr_lines(apply_src, "append_commit")
    append_in_mat = _attr_lines(mat_src, "append_commit")
    assert append_in_apply == [], (
        "append_commit ne doit PAS apparaître directement dans apply_commit "
        f"(il vit dans _materialize_commit), vu {append_in_apply}"
    )
    assert len(append_in_mat) == 1, (
        f"append_commit attendu 1 fois dans _materialize_commit, vu {append_in_mat}"
    )

    # chemin FRAIS : le gate précède lexicalement la délégation à _materialize_commit.
    mat_calls_in_apply = _attr_lines(apply_src, "_materialize_commit")
    assert mat_calls_in_apply, "apply_commit doit déléguer à _materialize_commit"
    assert auth_in_apply[0] < max(mat_calls_in_apply), (
        "assert_commit_allowed (gate) doit précéder la matérialisation du chemin "
        "frais (gate-avant-journal)"
    )


# =============================================================================
# C1-C3 — injection de crash (FailingStorage)
# =============================================================================


async def test_crash_mid_promote_leaves_prior_version_intact() -> None:
    """C1 — crash sur le 1er put du bank vivant (JOURNAL-FIRST) : le journal
    commits/{N} EST présent (écrit AVANT le promote, source de roll-forward), le
    pointeur est encore N-1, le fichier prior est INTACT (jamais à demi-écrasé puis
    pointé) ; re-run réussit. RED si le pointeur avançait avant le promote (pointeur
    orphelin) OU si le journal était écrit APRÈS le promote (perte de la source de
    roll-forward sur crash mid-promote)."""
    storage = FailingStorage(fail_on_key_substr=f"{SPACE}/bank/", nth=1)
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")
    # un fichier bank "prior" existe (version antérieure).
    storage.objects[f"{SPACE}/bank/activeContext.md"] = "PRIOR"

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidC1",
        staged={"activeContext.md": "NEW"}, event_id="evtC1",
    )
    with pytest.raises(RuntimeError):
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert storage.tripped
    # JOURNAL-FIRST : le journal est présent à N=0 (écrit AVANT le promote), donc le
    # roll-forward a une source durable. Le pointeur est ENCORE -1 (le flip est en
    # étape 5, jamais atteint). Le fichier prior est INTACT : le put qui l'aurait
    # écrasé est précisément celui qui a échoué (jamais de bank partiel pointé).
    assert (await store.get_commit(0)).commit_id == "cidC1"
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert storage.objects[f"{SPACE}/bank/activeContext.md"] == "PRIOR"

    # re-run (roll-forward) sur un storage sain (même état, staging intact) -> le
    # promote est RE-MATÉRIALISÉ idempotemment, le pointeur avance à 0 (autoritaire).
    healthy = FakeStorage()
    healthy.objects.update(storage.objects)
    store2, q2, l2, rt2 = _runtime(healthy, clock)
    ptr = await rt2.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    assert ptr.bank_version == 0
    assert healthy.objects[f"{SPACE}/bank/activeContext.md"] == "NEW"


async def test_crash_after_append_before_pointer_recovers_via_rebuild() -> None:
    """C2 — crash sur le put du pointeur : commits/ a N mais pointeur à N-1 ;
    rebuild_pointer_from_commits récupère N ; pas de trou. RED si le journal
    n'était pas écrit AVANT le pointeur (pas de source de récupération)."""
    # nth=2 : la 1ʳᵉ écriture du pointeur est le seeding (bank_version=-1), la 2ᵉ
    # est le flip de l'apply (point de linéarisation) -> c'est elle qui crashe.
    storage = FailingStorage(
        fail_on_key_substr=layout.bank_version_key(SPACE), nth=2
    )
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidC2",
        staged={"a.md": "A"}, event_id="evtC2",
    )
    with pytest.raises(RuntimeError):
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert storage.tripped
    # journal présent à N=0, pointeur encore -1 (le put du pointeur a échoué).
    assert (await store.get_commit(0)).commit_id == "cidC2"
    assert (await store.get_bank_version_pointer()).bank_version == -1
    # recovery sanctionnée (sur storage sain, post-restart) : rebuild depuis les
    # commits récupère la bonne version — pas de trou, le journal est la source.
    healthy = FakeStorage()
    healthy.objects.update(storage.objects)
    store2 = make_store(healthy)
    recovered = await store2.rebuild_pointer_from_commits()
    assert recovered is not None and recovered.bank_version == 0
    assert recovered.commit_id == "cidC2"


async def test_crash_after_pointer_before_watermark_keeps_tombstone() -> None:
    """C3 — crash sur le put du watermark : version autoritaire mais watermark en
    retard -> gc_tombstones n'évince pas le nouveau tombstone (min < N) ; la REPRISE
    roll-forward (re-apply du même commit) complète watermark + release token SANS
    VERSION_CONFLICT. RED si le watermark précédait le pointeur (le curseur de
    progrès dépasserait la version réelle) OU si le re-apply ne détectait pas
    « déjà appliqué » (il lèverait VERSION_CONFLICT au lieu de finir l'apply)."""
    storage = FailingStorage(
        fail_on_key_substr=layout.watermark_prefix(SPACE), nth=1
    )
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidC3",
        staged={"a.md": "A"}, event_id="evtC3", notes_consumed=["n1"],
    )
    with pytest.raises(RuntimeError):
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert storage.tripped
    # pointeur déjà avancé à 0 (POINT DE LINÉARISATION franchi), tombstone écrit,
    # mais watermark absent (le put a échoué).
    assert (await store.get_bank_version_pointer()).bank_version == 0
    assert await store.get_tombstone("n1") is not None
    assert await store.get_watermark("nodeA") is None
    # GC : aucun watermark -> min == -1 -> n'évince pas le tombstone.
    assert await commit_rt.gc_tombstones(expected_node_ids={"nodeA"}) == 0
    assert await store.get_tombstone("n1") is not None

    # re-run sur storage sain : la REPRISE roll-forward complète le watermark +
    # release token SANS VERSION_CONFLICT. Le pointeur nomme déjà ce commit
    # (bank_version=0, commit_id=cidC3) -> apply_commit détecte « déjà appliqué » et
    # finit idempotemment les étapes post-pointeur, sans ré-entrer G0/G2 (qui
    # lèverait VERSION_CONFLICT : pointeur=0 vs intent.previous=-1).
    healthy = FakeStorage()
    healthy.objects.update(storage.objects)
    store2, q2, l2, rt2 = _runtime(healthy, clock)
    # pré-condition : watermark absent, token encore HELD par nodeA (crash AVANT 9).
    assert await store2.get_watermark("nodeA") is None
    tok_before = await store2.get_token()
    assert tok_before is not None and tok_before.state == TokenState.HELD.value
    ptr = await rt2.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    assert ptr.bank_version == 0  # pointeur inchangé, aucun rollback
    # roll-forward complété : watermark avancé + token release (convergence).
    assert (await store2.get_watermark("nodeA")).bank_version == 0
    assert (await store2.get_token()).state == TokenState.FREE.value


# =============================================================================
# C4-C5 — atomicité crash bank vivant multi-fichiers (finding Codex PR #99 #2)
# =============================================================================


async def test_crash_mid_promote_multifile_rolls_forward_to_coherent_state() -> None:
    """C4 (finding 2 — atomicité bank vivant) — manifest MULTI-FICHIERS où le 1er
    put live RÉUSSIT et un put live ULTÉRIEUR ÉCHOUE. On prouve qu'à la frontière :

    - le journal commits/{N} est présent (JOURNAL-FIRST, source de roll-forward) ;
    - le pointeur est encore N-1 (le flip est en étape 5, jamais atteint) ;
    - le 1er fichier a bien été promu (live) MAIS le fichier prior que le put
      ÉCHOUÉ aurait écrasé reste INTACT (jamais corrompu) ;
    puis qu'un re-run d'apply_commit (roll-forward) RE-MATÉRIALISE le promote
    idempotemment et atteint un état pointeur+bank COHÉRENT (pointeur=N, tous les
    fichiers de N en live).

    RED-without : si le journal était écrit APRÈS le promote (ancien ordre), le
    journal serait ABSENT à la frontière (perte de la source de roll-forward) ; si
    le pointeur avançait avant la fin du promote, on aurait un pointeur orphelin
    nommant N sur un bank partiel. GREEN-with : journal-first + pointeur-last.
    """
    # nth=2 : le 1er put live (clé `{SPACE}/bank/`) réussit, le 2ᵉ échoue. Les deux
    # paths du manifest sont triés (a.md < z.md), donc a.md est promu, z.md échoue.
    storage = FailingStorage(fail_on_key_substr=f"{SPACE}/bank/", nth=2)
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")
    # fichiers "prior" pour les DEUX paths (version antérieure du bank).
    storage.objects[f"{SPACE}/bank/a.md"] = "A_PRIOR"
    storage.objects[f"{SPACE}/bank/z.md"] = "Z_PRIOR"

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidC4",
        staged={"a.md": "A_NEW", "z.md": "Z_NEW"}, event_id="evtC4",
    )
    with pytest.raises(RuntimeError):
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert storage.tripped
    # JOURNAL-FIRST : commits/0 présent (source durable de roll-forward).
    assert (await store.get_commit(0)).commit_id == "cidC4"
    # pointeur encore N-1 : pas de flip, pas de pointeur orphelin.
    assert (await store.get_bank_version_pointer()).bank_version == -1
    # 1er put réussi (a.md promu) ; 2ᵉ put échoué -> z.md PRIOR INTACT (jamais
    # corrompu/à-demi-écrit). C'est l'état partiel transitoire documenté (risque
    # résiduel borné), mais le prior NON écrasé n'est jamais corrompu.
    assert storage.objects[f"{SPACE}/bank/a.md"] == "A_NEW"
    assert storage.objects[f"{SPACE}/bank/z.md"] == "Z_PRIOR"

    # re-run (roll-forward) sur storage sain, MÊME staging intact -> re-promote
    # idempotent de TOUTES les entrées + flip pointeur -> état cohérent.
    healthy = FakeStorage()
    healthy.objects.update(storage.objects)
    store2, q2, l2, rt2 = _runtime(healthy, clock)
    ptr = await rt2.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    # COHÉRENCE pointeur+bank : pointeur=0 ET les deux fichiers de N en live.
    assert ptr.bank_version == 0
    assert (await store2.get_bank_version_pointer()).bank_version == 0
    assert healthy.objects[f"{SPACE}/bank/a.md"] == "A_NEW"
    assert healthy.objects[f"{SPACE}/bank/z.md"] == "Z_NEW"


async def test_crash_on_append_commit_leaves_live_bank_untouched() -> None:
    """C5 (finding 2 — JOURNAL-FIRST, preuve d'ordre) — si l'écriture du JOURNAL
    (append_commit) elle-même échoue, AUCUN fichier du bank vivant n'a été muté et
    le pointeur reste N-1. Prouve que le journal précède STRICTEMENT le promote :
    rien de live ne bouge tant que le record durable n'est pas posé.

    RED-without : dans l'ancien ordre (promote-first), le promote aurait DÉJÀ écrit
    le bank vivant avant que le journal n'échoue -> assertion `a.md` absent du live
    échouerait. GREEN-with : journal-first -> live intact, zéro mutation observable.
    """
    storage = FailingStorage(
        fail_on_key_substr=layout.commit_key(SPACE, 0), nth=1
    )
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidC5",
        staged={"a.md": "A_NEW"}, event_id="evtC5",
    )
    with pytest.raises(RuntimeError):
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert storage.tripped
    # le journal a échoué -> il n'existe pas ; le bank vivant n'a PAS été touché
    # (journal-first : promote n'a jamais commencé) ; pointeur encore -1.
    assert await store.get_commit(0) is None
    assert f"{SPACE}/bank/a.md" not in storage.objects
    assert (await store.get_bank_version_pointer()).bank_version == -1


# =============================================================================
# C6-C8 — resume post-pointeur : vérification du commit durable (finding #1)
# =============================================================================


def test_assert_durable_commit_matches_pure() -> None:
    """C6 (finding 1 — couche pure) — `assert_durable_commit_matches` passe sur un
    commit identique au record durable, et FERME (RESUME_COMMIT_DIVERGENT) dès
    qu'un champ définissant le commit diverge (commit_id, parent, term,
    membership_epoch, notes_consumed, manifest). RED si l'un de ces champs n'était
    pas comparé (un payload divergent passerait en silence)."""
    durable = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="cidR",
        staged={"a.md": "A"}, notes_consumed=["n1"],
    )
    assert_durable_commit_matches(durable, durable)  # identique -> ne lève pas

    # manifest divergent (octets différents -> sha256/size différents).
    div_manifest = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="cidR",
        staged={"a.md": "B"}, notes_consumed=["n1"],
    )
    with pytest.raises(CommitApplyError) as e1:
        assert_durable_commit_matches(div_manifest, durable)
    assert e1.value.reason == CommitApplyReason.RESUME_COMMIT_DIVERGENT
    assert "manifest" in e1.value.details["fields"]

    # notes_consumed divergent.
    div_notes = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="cidR",
        staged={"a.md": "A"}, notes_consumed=["n1", "n2"],
    )
    with pytest.raises(CommitApplyError) as e2:
        assert_durable_commit_matches(div_notes, durable)
    assert "notes_consumed" in e2.value.details["fields"]

    # term divergent.
    div_term = make_commit(
        bank_version=0, parent_bank_version=-1, term=3, commit_id="cidR",
        staged={"a.md": "A"}, notes_consumed=["n1"],
    )
    with pytest.raises(CommitApplyError) as e3:
        assert_durable_commit_matches(div_term, durable)
    assert "term" in e3.value.details["fields"]


async def test_resume_divergent_commit_fails_closed_no_mutation() -> None:
    """C7 (finding 1 — resume divergent) — le pointeur nomme (bank_version=0,
    commit_id=cidR) et commits/0 durable porte un manifest/notes donné. Un re-apply
    avec un BankCommit qui partage (bank_version, commit_id) MAIS DIVERGE (manifest
    + notes_consumed) FERME en RESUME_COMMIT_DIVERGENT et ne mute RIEN (pas de
    tombstone du payload divergent, watermark/token inchangés).

    RED-without : l'ancien chemin de resume appelait `_finish_post_pointer` sur le
    BankCommit FOURNI sans charger/vérifier commits/0 -> le tombstone `evil` du
    payload divergent serait créé, le token release, etc. GREEN-with : on charge le
    record durable et on ferme sur divergence avant toute mutation."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    # apply nominal d'un commit SAIN -> pointeur nomme (0, cidR), commits/0 durable.
    honest = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidR",
        staged={"a.md": "HONEST"}, event_id="evtR", notes_consumed=["n_honest"],
    )
    await commit_rt.apply_commit(
        honest, good_intent_for(honest), local_node_id="nodeA", fencing_token=2
    )
    assert (await store.get_bank_version_pointer()).commit_id == "cidR"
    before = storage.snapshot()

    # commit DIVERGENT : même (bank_version, commit_id) que le pointeur, mais
    # manifest + notes_consumed différents (payload in-memory/réseau falsifié).
    evil = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="cidR",
        staged={"a.md": "EVIL"}, event_id="evtR", notes_consumed=["evil_note"],
    )
    with pytest.raises(CommitApplyError) as e:
        await commit_rt.apply_commit(
            evil, good_intent_for(evil), local_node_id="nodeA", fencing_token=2
        )
    assert e.value.reason == CommitApplyReason.RESUME_COMMIT_DIVERGENT
    # ZÉRO mutation par le payload divergent : pas de tombstone `evil_note`,
    # le tombstone honnête survit, et l'état global n'a pas bougé.
    assert await store.get_tombstone("evil_note") is None
    assert await store.get_tombstone("n_honest") is not None
    assert storage.snapshot() == before


async def test_resume_missing_durable_commit_fails_closed() -> None:
    """C8 (finding 1 — resume sans journal) — le pointeur nomme (0, cidR) mais le
    record durable commits/0 est ABSENT (état critique incohérent). Le resume FERME
    en CorruptedStateError (jamais réparé en silence, jamais de mutation sur la base
    du seul pointeur). RED-without : l'ancien chemin appelait directement
    `_finish_post_pointer` sans charger commits/0 -> il muterait l'état sur un
    pointeur sans journal."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")
    # pointeur posé manuellement à (0, cidR) SANS écrire commits/0 (incohérence).
    await store.set_bank_version_pointer(
        BankVersionPointer(bank_version=0, commit_id="cidR")
    )
    assert await store.get_commit(0) is None  # journal durable absent

    c0 = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="cidR",
        staged={"a.md": "A"}, event_id="evtR",
    )
    with pytest.raises(CorruptedStateError):
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )


async def test_resume_matching_durable_commit_completes_roll_forward() -> None:
    """C9 (finding 1 — resume sain) — quand le commit fourni ÉGALE le record durable
    commits/0, le resume COMPLÈTE idempotemment les étapes post-pointeur (watermark,
    release token) sans VERSION_CONFLICT. Garantit que la vérification durcie ne
    casse PAS le roll-forward légitime (chemin GREEN du finding 1)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidR9",
        staged={"a.md": "A"}, event_id="evtR9", notes_consumed=["n9"],
    )
    await commit_rt.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    # re-apply roll-forward avec le MÊME commit (égal au durable) -> no-op cohérent.
    ptr = await commit_rt.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    assert ptr.bank_version == 0
    assert (await store.get_watermark("nodeA")).bank_version == 0
    assert (await store.get_token()).state == TokenState.FREE.value
    assert await store.get_tombstone("n9") is not None


# =============================================================================
# C10-C13 — findings Codex PR #99 (pr99.out) : roll-forward sans lease vivant
#           (finding 1) + marqueur de publication MANIFEST.json (finding 2)
# =============================================================================


def test_assert_staging_manifest_matches_pure() -> None:
    """C10 (finding 2 — couche pure) — `assert_staging_manifest_matches` PASSE sur
    un manifest stagé identique au commit, FERME en STAGING_MANIFEST_MISSING quand
    le marqueur est ABSENT (None), et en STAGING_MANIFEST_DIVERGENT dès qu'un champ
    définissant (bank_version inclus, car la clé de lecture est le commit_id)
    diverge. RED si l'absence ou la divergence n'était pas fermée."""
    commit = make_commit(
        bank_version=1, parent_bank_version=0, term=2, commit_id="cidM",
        staged={"a.md": "A"}, notes_consumed=["n1"],
    )
    # identique -> ne lève pas.
    assert_staging_manifest_matches(commit, commit)

    # marqueur ABSENT -> STAGING_MANIFEST_MISSING.
    with pytest.raises(CommitApplyError) as e_missing:
        assert_staging_manifest_matches(commit, None)
    assert e_missing.value.reason == CommitApplyReason.STAGING_MANIFEST_MISSING

    # bank_version divergent (la clé de lecture est commit_id, donc bank_version
    # DOIT être comparé) -> STAGING_MANIFEST_DIVERGENT.
    div_bv = make_commit(
        bank_version=2, parent_bank_version=1, term=2, commit_id="cidM",
        staged={"a.md": "A"}, notes_consumed=["n1"],
    )
    with pytest.raises(CommitApplyError) as e_bv:
        assert_staging_manifest_matches(commit, div_bv)
    assert e_bv.value.reason == CommitApplyReason.STAGING_MANIFEST_DIVERGENT
    assert "bank_version" in e_bv.value.details["fields"]

    # manifest divergent (octets différents -> sha256/size) -> DIVERGENT.
    div_manifest = make_commit(
        bank_version=1, parent_bank_version=0, term=2, commit_id="cidM",
        staged={"a.md": "DIFFERENT"}, notes_consumed=["n1"],
    )
    with pytest.raises(CommitApplyError) as e_man:
        assert_staging_manifest_matches(commit, div_manifest)
    assert e_man.value.reason == CommitApplyReason.STAGING_MANIFEST_DIVERGENT
    assert "manifest" in e_man.value.details["fields"]


async def test_apply_missing_staging_manifest_marker_fails_closed() -> None:
    """C11 (finding 2 — end-to-end) — un arbre stagé dont le MARQUEUR DE PUBLICATION
    staging/{commit_id}/MANIFEST.json est ABSENT (crash de stage_commit avant le
    manifest-last) fait FERMER l'apply en STAGING_MANIFEST_MISSING, AVANT toute
    mutation. Les fichiers bank stagés EXISTENT (load_staged + verify_manifest
    passeraient), donc seul le contrôle du marqueur ferme le commit.

    RED-without : sans la vérif du marqueur en G3a, l'apply lit les fichiers bank
    stagés, valide le manifest fourni contre eux, et APPLIQUE un stage jamais publié
    (pointeur avance, bank promu) — fail-OPEN sur un stage incomplet. GREEN-with :
    le marqueur absent ferme le commit, zéro mutation."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidNoMan",
        staged={"a.md": "A"}, event_id="evtNoMan", notes_consumed=["n1"],
    )
    # simuler un crash de stage_commit AVANT le manifest-last : on supprime le
    # marqueur de publication MAIS on laisse l'arbre bank stagé intact.
    manifest_key = layout.staging_manifest_key(SPACE, "cidNoMan")
    assert manifest_key in storage.objects  # stage_commit l'a bien écrit
    del storage.objects[manifest_key]
    # le fichier bank stagé, lui, est toujours là (stage partiel).
    assert layout.staging_bank_key(SPACE, "cidNoMan", "a.md") in storage.objects

    before = storage.snapshot()
    with pytest.raises(CommitApplyError) as e:
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert e.value.reason == CommitApplyReason.STAGING_MANIFEST_MISSING
    # ZÉRO mutation : pas de journal, pas de bank vivant, pointeur -1, token HELD.
    assert await store.get_commit(0) is None
    assert f"{SPACE}/bank/a.md" not in storage.objects
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert await store.get_tombstone("n1") is None
    assert (await store.get_token()).state == TokenState.HELD.value
    assert storage.snapshot() == before


async def test_apply_divergent_staging_manifest_marker_fails_closed() -> None:
    """C12 (finding 2 — marqueur divergent) — si le MANIFEST.json stagé existe mais
    NOMME un autre commit (commit_id/forme différents) que le BankCommit fourni,
    l'apply FERME en STAGING_MANIFEST_DIVERGENT. Couvre le cas « un caller présente un
    commit cohérent avec les fichiers bank d'un AUTRE stage publié ».

    RED-without : sans la comparaison marqueur<->commit, le mismatch passerait
    inaperçu (load_staged validerait juste les octets du manifest fourni)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidDiv",
        staged={"a.md": "A"}, event_id="evtDiv",
    )
    # overwrite du marqueur publié avec un MANIFEST.json nommant un autre commit_id
    # (forme valide mais divergente), sous la MÊME clé de staging.
    other = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="cidOTHER",
        staged={"a.md": "A"}, event_id="evtDiv",
    )
    await storage.put_json(
        layout.staging_manifest_key(SPACE, "cidDiv"), other.model_dump(mode="json")
    )

    before = storage.snapshot()
    with pytest.raises(CommitApplyError) as e:
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert e.value.reason == CommitApplyReason.STAGING_MANIFEST_DIVERGENT
    assert "commit_id" in e.value.details["fields"]
    # zéro mutation.
    assert await store.get_commit(0) is None
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert storage.snapshot() == before


async def test_durable_commit_rolls_forward_after_lease_expired() -> None:
    """C13 (finding 1 — roll-forward PRÉ-pointeur SANS lease vivant) — un commit
    DURABLE (commits/{N} écrit JOURNAL-FIRST, pointeur ENCORE à N-1 après un crash
    mid-promote) DOIT pouvoir être COMPLÉTÉ par roll-forward MÊME APRÈS l'expiration
    du lease du holder originel. La reprise FINIT un apply DÉJÀ AUTORISÉ (le lease
    était vivant quand le commit a été journalisé) — elle ne doit PAS re-exiger un
    lease vivant.

    RED-without : sans le chemin de reprise pré-pointeur ancré sur le record durable,
    le re-run retombe sur G0/assert_commit_allowed -> lease expiré -> FENCED, laissant
    un commit durable ORPHELIN (pointeur bloqué à N-1, bank jamais matérialisé) jusqu'à
    une nouvelle acquisition. On PROUVE le RED en montrant que le MÊME intent sous le
    lease expiré est bien rejeté FENCED par assert_commit_allowed.

    GREEN-with : le roll-forward se déclenche sur le record durable matchant, promeut
    le bank, flippe le pointeur, finit watermark + convergence token — SANS lease."""
    # crash sur le 1er put du bank vivant : journal présent, pointeur -1, bank non promu.
    storage = FailingStorage(fail_on_key_substr=f"{SPACE}/bank/", nth=1)
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    # lease vivant court (300s) au moment de la 1ʳᵉ tentative.
    await seed_holder(
        store, clock, term=2, bank_version=-1, holder="nodeA", lease_seconds_ahead=300
    )

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidRF",
        staged={"a.md": "NEW"}, event_id="evtRF", notes_consumed=["n_rf"],
    )
    with pytest.raises(RuntimeError):
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert storage.tripped
    # JOURNAL-FIRST : commits/0 durable, pointeur ENCORE -1 (flip jamais atteint).
    assert (await store.get_commit(0)).commit_id == "cidRF"
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert f"{SPACE}/bank/a.md" not in storage.objects  # promote jamais commencé

    # restart sur storage sain (même état, staging + journal intacts).
    healthy = FakeStorage()
    healthy.objects.update(storage.objects)
    store2, q2, l2, rt2 = _runtime(healthy, clock)

    # LE LEASE EXPIRE entre le crash et la reprise (au-delà des 300s).
    clock.tick(seconds=600)

    # RED-PROOF : sous le lease expiré, l'autorisation FRAÎCHE (G0) rejette FENCED.
    # C'est exactement le chemin qu'empruntait l'ancien code au re-run -> orphelin.
    with pytest.raises(CommitNotAuthorized) as e_auth:
        await l2.assert_commit_allowed(good_intent_for(c0))
    assert e_auth.value.reason == CommitDenyReason.FENCED

    # GREEN : le roll-forward pré-pointeur COMPLÈTE l'apply SANS lease vivant.
    ptr = await rt2.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    assert ptr.bank_version == 0
    # bank matérialisé, pointeur autoritaire, watermark avancé, tombstone, token libéré.
    assert healthy.objects[f"{SPACE}/bank/a.md"] == "NEW"
    assert (await store2.get_bank_version_pointer()).bank_version == 0
    assert (await store2.get_watermark("nodeA")).bank_version == 0
    assert await store2.get_tombstone("n_rf") is not None
    assert (await store2.get_token()).state == TokenState.FREE.value


async def test_fresh_commit_still_requires_live_lease() -> None:
    """C14 (finding 1 — garde : l'autorisation FRAÎCHE n'est PAS affaiblie) — un
    commit FRAIS (aucun record durable commits/{N}) sous un lease EXPIRÉ DOIT être
    rejeté FENCED par G0. Le roll-forward pré-pointeur ne s'applique QU'à un commit
    déjà journalisé : il ne doit jamais ouvrir une porte pour un commit jamais
    autorisé.

    RED-without : si le chemin de reprise se déclenchait sans exiger un record durable
    matchant (ou sautait G0 inconditionnellement), un commit frais sous lease expiré
    passerait — régression de sécurité. GREEN-with : pas de commits/{N} -> on tombe sur
    G0 -> FENCED, zéro mutation."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(
        store, clock, term=2, bank_version=-1, holder="nodeA", lease_seconds_ahead=300
    )

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidFresh",
        staged={"a.md": "A"}, event_id="evtFresh",
    )
    assert await store.get_commit(0) is None  # JAMAIS journalisé (commit frais)
    clock.tick(seconds=600)  # lease expiré

    before = storage.snapshot()
    with pytest.raises(CommitNotAuthorized) as e:
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert e.value.reason == CommitDenyReason.FENCED
    # zéro mutation : pas de journal, pas de bank, pointeur -1.
    assert await store.get_commit(0) is None
    assert f"{SPACE}/bank/a.md" not in storage.objects
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert storage.snapshot() == before


# =============================================================================
# A12-A14 — résolution des trois findings Codex (PR #99)
# =============================================================================


async def test_remote_peer_apply_converges_token_to_free() -> None:
    """A12 (finding 1 — convergence token) — un PAIR RÉEL (nodeB) qui applique un
    BANK_COMMIT authoré par nodeA (holder) DOIT faire converger son token.json
    local de HELD(nodeA) vers FREE. RED sans la convergence : l'ancien code gatait
    le release sur ``local_node_id == committed_by_node_id`` (donc nodeB laissait
    son token HELD par nodeA, divergent, bloquant le prochain acquire jusqu'à
    l'expiration de lease)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    # État RÉPLIQUÉ sur le pair nodeB : term=2, pointeur=-1, token HELD par nodeA
    # (le holder distant). nodeB n'est PAS le holder.
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidB",
        staged={"a.md": "A"}, committed_by="nodeA", event_id="evtB",
    )
    # apply côté PAIR : local_node_id=nodeB != committed_by_node_id=nodeA.
    ptr = await commit_rt.apply_commit(
        c0, good_intent_for(c0, holder="nodeA"),
        local_node_id="nodeB", fencing_token=2,
    )
    assert ptr.bank_version == 0
    # le pair a son watermark à lui (nodeB), pas celui du holder.
    assert (await store.get_watermark("nodeB")).bank_version == 0
    assert await store.get_watermark("nodeA") is None
    # CONVERGENCE : le token local du pair est FREE (le holder a release dans le
    # commit) ; term/fencing préservés (release ne descend jamais).
    tok = await store.get_token()
    assert tok is not None and tok.state == TokenState.FREE.value
    assert tok.term == 2 and tok.fencing_token == 2


async def test_converge_token_release_is_idempotent_and_monotone() -> None:
    """A12b — la convergence ne dégrade JAMAIS un grant postérieur : si, depuis le
    commit, le token a été RE-ACQUIS à un term plus récent (nouveau holder), un
    re-apply roll-forward du vieux commit NE libère PAS le token courant. RED si la
    convergence libérait inconditionnellement (elle écraserait le nouveau holder)."""
    from datetime import timedelta

    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidMono",
        staged={"a.md": "A"}, committed_by="nodeA", event_id="evtMono",
    )
    await commit_rt.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    assert (await store.get_token()).state == TokenState.FREE.value

    # un NOUVEAU grant arrive : term 3, holder nodeC, HELD vivant.
    await store.set_token(
        held_token(
            holder="nodeC", term=3,
            lease_until=(clock.now() + timedelta(seconds=300)).isoformat(),
        )
    )
    # re-apply roll-forward du VIEUX commit (pointeur nomme déjà cidMono@0).
    await commit_rt.apply_commit(
        c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
    )
    # le nouveau grant survit : la convergence ne touche que HELD(nodeA, term=2).
    tok = await store.get_token()
    assert tok.state == TokenState.HELD.value
    assert tok.holder_node_id == "nodeC" and tok.term == 3


async def test_extra_staged_file_outside_manifest_fails_closed_end_to_end() -> None:
    """A13 (finding 3 — PARTIAL_STAGE) — un objet stagé sous
    staging/{commit_id}/bank/ mais ABSENT du manifest fait FERMER l'apply réel
    (PARTIAL_STAGE) et ne mute RIEN. RED sans la LIST réelle des objets stagés :
    load_staged ne lit que les paths du manifest, donc l'extra serait invisible,
    l'apply réussirait et l'objet serait abandonné sous le staging."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidX",
        staged={"a.md": "A"}, event_id="evtX",
    )
    # injecter un objet stagé EN PLUS, hors manifest, sous le même commit_id.
    await storage.put(
        layout.staging_bank_key(SPACE, "cidX", "rogue.md"), "ROGUE"
    )

    before = storage.snapshot()
    with pytest.raises(CommitApplyError) as e:
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )
    assert e.value.reason == CommitApplyReason.PARTIAL_STAGE
    assert "rogue.md" in e.value.details["extra_paths"]
    # ZÉRO mutation : pas de bank vivant, pas de commit, pointeur -1, token HELD.
    assert f"{SPACE}/bank/a.md" not in storage.objects
    assert await store.get_commit(0) is None
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert (await store.get_token()).state == TokenState.HELD.value
    assert storage.snapshot() == before  # rien n'a bougé


async def test_apply_fencing_token_must_match_intent() -> None:
    """A14 (finding MINOR) — le ``fencing_token`` explicite n'est plus une surface
    morte : s'il diverge de ``intent.fencing_token`` (la source d'autorisation),
    l'apply FERME en FENCING_TOKEN_MISMATCH AVANT toute mutation. RED si l'argument
    était silencieusement ignoré (apply réussirait avec un fencing incohérent)."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidF",
        staged={"a.md": "A"}, event_id="evtF",
    )
    before = storage.snapshot()
    with pytest.raises(CommitApplyError) as e:
        await commit_rt.apply_commit(
            c0, good_intent_for(c0),  # intent.fencing_token == 2
            local_node_id="nodeA", fencing_token=99,  # explicite divergent
        )
    assert e.value.reason == CommitApplyReason.FENCING_TOKEN_MISMATCH
    # zéro mutation : ferme avant G0/apply.
    assert await store.get_commit(0) is None
    assert (await store.get_bank_version_pointer()).bank_version == -1
    assert storage.snapshot() == before


# =============================================================================
# A-corruption — fail-closed sur état corrompu (CorruptedStateError propage)
# =============================================================================


@pytest.mark.parametrize("target", ["token", "term", "pointer"])
async def test_apply_corruption_propagates(target: str) -> None:
    """Corruption d'un objet d'état critique -> CorruptedStateError PROPAGE via le
    gate (jamais CommitNotAuthorized, jamais default-allow). RED si un try/except
    avalait la corruption."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store, queue, lease, commit_rt = _runtime(storage, clock)
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")

    c0 = await _stage_and_commit(
        commit_rt, bank_version=0, parent_bank_version=-1, term=2, commit_id="cidX",
        staged={"a.md": "A"}, event_id="evtX",
    )
    key = {
        "token": layout.token_key(SPACE),
        "term": layout.term_key(SPACE),
        "pointer": layout.bank_version_key(SPACE),
    }[target]
    storage.objects[key] = "{not valid json"

    with pytest.raises(CorruptedStateError):
        await commit_rt.apply_commit(
            c0, good_intent_for(c0), local_node_id="nodeA", fencing_token=2
        )


# =============================================================================
# G-AST + gardes constructeur
# =============================================================================


def test_commit_runtime_does_not_import_graph_or_consolidation() -> None:
    """G-AST — RED si un import graph/long entre dans le chemin de commit
    (invariant ADR-0011/0012 + posture no-timer). Scan AST des imports."""
    source = inspect.getsource(commit_runtime_module)
    forbidden = ("graph_push", "graph", "long", "consolidator", "consolidation_queue")

    imported_modules: list[str] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported_modules.append(node.module or "")
            imported_modules += [alias.name for alias in node.names]
    for mod in imported_modules:
        for needle in forbidden:
            assert needle not in mod, (
                f"commit_runtime importe interdit: {mod!r} (contient {needle!r})"
            )


def test_commit_runtime_space_id_mismatch_rejected() -> None:
    """Gardes constructeur : space_id != store.space_id et != lease.space_id."""
    storage = FakeStorage()
    clock = DeterministicClock()
    store = make_store(storage, "alpha")
    queue = QueueRuntime(store, "alpha")
    lease = LeaseRuntime(store, "alpha", queue, clock=clock.now)
    # space_id != store.space_id
    with pytest.raises(ValueError):
        CommitRuntime(store, storage, "beta", lease)  # type: ignore[arg-type]
    # space_id != lease.space_id
    store_beta = make_store(FakeStorage(), "beta")
    queue_beta = QueueRuntime(store_beta, "beta")
    lease_beta = LeaseRuntime(store_beta, "beta", queue_beta, clock=clock.now)
    with pytest.raises(ValueError):
        CommitRuntime(store, storage, "alpha", lease_beta)  # type: ignore[arg-type]
