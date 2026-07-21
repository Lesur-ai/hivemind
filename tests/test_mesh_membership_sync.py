# -*- coding: utf-8 -*-
"""Project Mesh candidate-view digest + convergence helpers (P10-3, issue #191)."""

from __future__ import annotations

import pytest

from live_mem.core.hivemind.models import Member, MemberStatus, MembershipView
from live_mem.mesh.membership_sync import (
    candidate_view_digest,
    projected_promotion_view,
    roster_fully_acked,
)

_KA = "ed25519:" + "a" * 43
_KB = "ed25519:" + "b" * 43


def _view(epoch: int, members: list[Member]) -> MembershipView:
    return MembershipView(epoch=epoch, members=members)


def test_digest_is_lowercase_64_hex_and_deterministic() -> None:
    v = _view(2, [Member(node_id="a", public_key=_KA)])
    d1 = candidate_view_digest(v)
    d2 = candidate_view_digest(_view(2, [Member(node_id="a", public_key=_KA)]))
    assert len(d1) == 64 and d1 == d2


def test_digest_is_order_insensitive_across_member_permutations() -> None:
    m_a = Member(node_id="a", public_key=_KA)
    m_b = Member(node_id="b", public_key=_KB, status=MemberStatus.PENDING)
    assert candidate_view_digest(_view(2, [m_a, m_b])) == candidate_view_digest(_view(2, [m_b, m_a]))


def test_digest_changes_on_epoch_status_scope_or_key() -> None:
    base = _view(2, [Member(node_id="a", public_key=_KA), Member(node_id="b", public_key=_KB, status=MemberStatus.PENDING)])
    d = candidate_view_digest(base)
    # epoch
    assert candidate_view_digest(_view(3, list(base.members))) != d
    # status (b pending -> active)
    active_b = _view(2, [Member(node_id="a", public_key=_KA), Member(node_id="b", public_key=_KB, status=MemberStatus.ACTIVE)])
    assert candidate_view_digest(active_b) != d
    # scope
    scoped = _view(2, [Member(node_id="a", public_key=_KA, scopes=["read"]), Member(node_id="b", public_key=_KB, status=MemberStatus.PENDING)])
    assert candidate_view_digest(scoped) != d
    # key
    keyed = _view(2, [Member(node_id="a", public_key="ed25519:" + "c" * 43), Member(node_id="b", public_key=_KB, status=MemberStatus.PENDING)])
    assert candidate_view_digest(keyed) != d


def test_digest_excludes_evicted_audit_records() -> None:
    # Two instances with divergent audit history (one carries an evicted record)
    # must still agree on the effective roster digest.
    with_evicted = _view(2, [
        Member(node_id="a", public_key=_KA),
        Member(node_id="z", public_key="ed25519:" + "d" * 43, status=MemberStatus.EVICTED),
    ])
    without = _view(2, [Member(node_id="a", public_key=_KA)])
    assert candidate_view_digest(with_evicted) == candidate_view_digest(without)


def test_source_and_target_agree_on_e2_digest() -> None:
    # Source e+1 view: source ACTIVE, target PENDING. Target imported the same
    # view. Both promote the target and must digest an identical e+2 view.
    e1_members = [
        Member(node_id="src", public_key=_KA),
        Member(node_id="tgt", public_key=_KB, status=MemberStatus.PENDING),
    ]
    source_e1 = _view(2, e1_members)
    target_e1 = _view(2, [Member(node_id="src", public_key=_KA), Member(node_id="tgt", public_key=_KB, status=MemberStatus.PENDING)])
    source_e2 = projected_promotion_view(source_e1, "tgt")
    target_e2 = projected_promotion_view(target_e1, "tgt")
    assert source_e2.epoch == 3 and target_e2.epoch == 3
    assert candidate_view_digest(source_e2) == candidate_view_digest(target_e2)


def test_projected_promotion_requires_pending() -> None:
    v = _view(2, [Member(node_id="a", public_key=_KA)])  # a is ACTIVE
    with pytest.raises(ValueError):
        projected_promotion_view(v, "a")
    with pytest.raises(ValueError):
        projected_promotion_view(v, "ghost")


def test_roster_fully_acked_is_set_identity_no_quorum() -> None:
    assert roster_fully_acked({"a", "b"}, {"a", "b"}) is True
    assert roster_fully_acked({"a"}, {"a", "b"}) is False  # one missing != success
    assert roster_fully_acked(set(), set()) is False  # empty roster is never success
    assert roster_fully_acked({"a"}, {"a"}) is True  # 2-node bootstrap: sole source
