# -*- coding: utf-8 -*-
"""
P5-8 (issue #16) — the P5-7 cross-seam: ``apply_commit`` -> ``reap_on_tombstone``.

A commit that tombstones a note must reap that note's surviving live copy so
``assert_no_tombstone_resurrection`` stays green. ``CommitRuntime`` gains an
OPTIONAL injected ``NoteReplicationRuntime``; when present, the apply step that
records a tombstone (step 3) also reaps the live copy. Optional so the P5-6
commit tests that build ``CommitRuntime`` without it are byte-for-byte unchanged.

Deterministic, OFFLINE, FakeStorage-backed. No real S3 / network / LLM.
"""

from __future__ import annotations

import pytest

from live_mem.core.hivemind import (
    CommitRuntime,
    HivemindStateStore,
    LeaseRuntime,
    QueueRuntime,
    Tombstone,
    build_commit_intent,
)
from live_mem.core.hivemind.note_replication import (
    NoteReplicationRuntime,
    ReplicatedNote,
    ReplicationStatus,
)
from tests.hivemind_harness import DeterministicClock
from tests.test_hivemind_commit import (
    SPACE,
    make_commit,
    make_store,
    seed_holder,
)
from tests.test_hivemind_state import FakeStorage


def _note_md(stem: str) -> str:
    return (
        "---\n"
        'timestamp: "2026-01-01T00:00:00+00:00"\n'
        'agent: "cline"\n'
        'category: "observation"\n'
        "tags: []\n"
        f'space_id: "{SPACE}"\n'
        "---\n\n"
        "peer note body"
    )


def _make_note(stem: str, origin_node_id: str) -> ReplicatedNote:
    return ReplicatedNote(
        note_id=stem,
        filename=f"{stem}.md",
        origin_node_id=origin_node_id,
        origin_agent="cline",
        category="observation",
        content="peer note body",
        created_at="2026-01-01T00:00:00+00:00",
        note_md=_note_md(stem),
    )


def _runtime_with_reaper(storage: FakeStorage, clock: DeterministicClock):
    """(store, commit_rt, reaper) sharing one storage/clock, with the reaper
    INJECTED into the CommitRuntime (the P5-8 wiring)."""
    store = make_store(storage)
    queue = QueueRuntime(store, SPACE)
    lease = LeaseRuntime(store, SPACE, queue, clock=clock.now)
    reaper = NoteReplicationRuntime(store, storage, SPACE, clock=clock.now)  # type: ignore[arg-type]
    crt = CommitRuntime(
        store, storage, SPACE, lease, clock=clock.now, note_replication=reaper  # type: ignore[arg-type]
    )
    return store, crt, reaper


async def _live_note_id_set(storage: FakeStorage) -> set[str]:
    """Live note ids present on this store (oracle's view: live/*.md minus
    sidecars/.keep)."""
    prefix = f"{SPACE}/live/"
    out: set[str] = set()
    for obj in await storage.list_objects(prefix):
        rel = obj["Key"][len(prefix):]
        if not rel.endswith(".md") or rel.startswith("_origin/"):
            continue
        out.add(rel[: -len(".md")])
    return out


async def _assert_oracle_green(store: HivemindStateStore, storage: FakeStorage) -> None:
    """No tombstoned note_id survives as a live copy (the single-store form of
    assert_no_tombstone_resurrection)."""
    tombstoned = {t.note_id for t in await store.list_tombstones()}
    live = await _live_note_id_set(storage)
    assert tombstoned.isdisjoint(live), (
        f"resurrection: tombstoned {tombstoned & live} still live"
    )


# =============================================================================
# 16. apply_commit reaps the peer's surviving live copy on tombstone
# =============================================================================


@pytest.mark.asyncio
async def test_apply_commit_reaps_peer_live_copy_on_tombstone() -> None:
    """A peer wrote live/{note}.md note-first; a commit tombstoning that note_id
    (with an injected NoteReplicationRuntime) reaps the live copy + sidecar, and
    assert_no_tombstone_resurrection stays green. RED without the reap wiring."""
    clock = DeterministicClock()
    storage = FakeStorage()
    store, crt, reaper = _runtime_with_reaper(storage, clock)

    stem = "20260101T000000_cline_observation_aa11bb22"

    # A peer copy exists (note-first reorder): replicate inbound writes a real
    # live/{filename} + provenance sidecar.
    note = _make_note(stem, origin_node_id="nodeB")
    r = await reaper.replicate_inbound(
        note=note, event_id="evt-1", event_ts="2026-01-01T00:00:01+00:00"
    )
    assert r.status == ReplicationStatus.STORED
    live_key = f"{SPACE}/live/{stem}.md"
    assert await storage.exists(live_key) is True

    # Seed a HELD holder + state, then commit a bank_version 0 that CONSUMES the
    # note (tombstones it).
    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")
    commit = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="c0",
        staged={"a.md": "A"}, committed_by="nodeA", notes_consumed=[stem],
    )
    intent = build_commit_intent(commit, holder_node_id="nodeA", fencing_token=2)
    await crt.stage_commit(
        commit_id="c0", proposed_bank=[("a.md", "A")], bank_version=0,
        parent_bank_version=-1, term=2, membership_epoch=1,
        committed_by_node_id="nodeA", event_id="evt-c", notes_consumed=[stem],
    )
    await crt.apply_commit(commit, intent, local_node_id="nodeA", fencing_token=2)

    # The tombstone exists AND the live copy was reaped -> oracle green.
    assert await store.get_tombstone(stem) is not None
    assert await storage.exists(live_key) is False
    await _assert_oracle_green(store, storage)


# =============================================================================
# 17. without the runtime -> unchanged (reap is a no-op seam, no AttributeError)
# =============================================================================


@pytest.mark.asyncio
async def test_apply_commit_without_replication_runtime_unchanged() -> None:
    """CommitRuntime built WITHOUT note_replication behaves exactly as P5-6: it
    tombstones but does NOT reap (no AttributeError). A pre-existing live copy
    survives — proving the reap is gated on the injected dep (and the P5-6 commit
    tests are unaffected)."""
    clock = DeterministicClock()
    storage = FakeStorage()
    store = make_store(storage)
    queue = QueueRuntime(store, SPACE)
    lease = LeaseRuntime(store, SPACE, queue, clock=clock.now)
    crt = CommitRuntime(store, storage, SPACE, lease, clock=clock.now)  # NO reaper

    stem = "20260101T000000_cline_observation_cc33dd44"
    live_key = f"{SPACE}/live/{stem}.md"
    await storage.put(live_key, _note_md(stem))  # a live copy exists

    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")
    commit = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="c0",
        staged={"a.md": "A"}, committed_by="nodeA", notes_consumed=[stem],
    )
    intent = build_commit_intent(commit, holder_node_id="nodeA", fencing_token=2)
    await crt.stage_commit(
        commit_id="c0", proposed_bank=[("a.md", "A")], bank_version=0,
        parent_bank_version=-1, term=2, membership_epoch=1,
        committed_by_node_id="nodeA", event_id="evt-c", notes_consumed=[stem],
    )
    ptr = await crt.apply_commit(commit, intent, local_node_id="nodeA", fencing_token=2)

    # Tombstone recorded; live copy NOT reaped (no injected runtime). No crash.
    assert ptr.bank_version == 0
    assert await store.get_tombstone(stem) is not None
    assert await storage.exists(live_key) is True


# =============================================================================
# 18. reap is idempotent on the writer node (live copy already gone)
# =============================================================================


@pytest.mark.asyncio
async def test_reap_idempotent_on_writer_node() -> None:
    """Writer-side: no live copy exists (the consolidator already removed it).
    apply_commit with an injected reaper succeeds; reap is a no-op, the commit
    applies cleanly, and the oracle is green."""
    clock = DeterministicClock()
    storage = FakeStorage()
    store, crt, _reaper = _runtime_with_reaper(storage, clock)

    stem = "20260101T000000_cline_observation_ee55ff66"
    # NO live/{stem}.md present (already consumed locally).

    await seed_holder(store, clock, term=2, bank_version=-1, holder="nodeA")
    commit = make_commit(
        bank_version=0, parent_bank_version=-1, term=2, commit_id="c0",
        staged={"a.md": "A"}, committed_by="nodeA", notes_consumed=[stem],
    )
    intent = build_commit_intent(commit, holder_node_id="nodeA", fencing_token=2)
    await crt.stage_commit(
        commit_id="c0", proposed_bank=[("a.md", "A")], bank_version=0,
        parent_bank_version=-1, term=2, membership_epoch=1,
        committed_by_node_id="nodeA", event_id="evt-c", notes_consumed=[stem],
    )
    ptr = await crt.apply_commit(commit, intent, local_node_id="nodeA", fencing_token=2)

    assert ptr.bank_version == 0
    assert await store.get_tombstone(stem) is not None
    await _assert_oracle_green(store, storage)
