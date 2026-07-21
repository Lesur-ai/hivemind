# -*- coding: utf-8 -*-
"""Project Mesh two-epoch membership convergence helpers (P10-3, issue #191).

Membership epochs advance as a **control-plane all-ACK-then-local-apply**
orchestration, never by delivering an epoch-advancing event across peers (the
strict epoch-equality fence in ``router.py`` / ``peer.py`` rejects that for
everyone).  After a full-mesh all-ACK over the source-signed **candidate-view
digest**, each active roster member self-applies its own fenced epoch bump.

The one asymmetric node is the enrolment **target**: it is ``PENDING``, holds no
local authority, and is excluded from every roster, so it cannot self-apply on
its own — it applies its e+2 activation only through the confined,
session-bound receive path guarded here (used by the ``router`` branch inside
``except _LocalUnsafe:``).

``candidate_view_digest`` is the deterministic agreement point: the source signs
it into the e+2 event and the target independently recomputes it from its own
local e+1 view (self ``PENDING`` -> ``ACTIVE``) and requires byte-for-byte
equality before self-activating, so the source can never promote the target into
a roster the target does not itself believe.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..core.hivemind.models import MemberStatus, MembershipView

#: Domain separator for the candidate-view digest (never reused elsewhere).
_CANDIDATE_VIEW_DOMAIN = b"hivemind-mesh-candidate-view-v1\0"
_ROSTER_STATUSES = (MemberStatus.ACTIVE.value, MemberStatus.PENDING.value)


def _canonical_json_bytes(value: Any) -> bytes:
    # Cross-instance-deterministic serialization (mirrors the existing
    # ``lifecycle._canonical_json_bytes`` / ``manifest_content_hash`` style so a
    # source and a target running the same code agree byte-for-byte).
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8", "strict")


def candidate_view_digest(view: MembershipView) -> str:
    """Return the lowercase 64-hex digest of a proposed membership view.

    The digest covers ONLY the ``ACTIVE`` ∪ ``PENDING`` roster (the members that
    matter for all-ACK / ordinary writes) plus the epoch and protocol version —
    audit-only ``evicted``/``leaving`` records are excluded so two instances with
    divergent audit history still agree on the effective roster.  Members are
    sorted by ``node_id`` and each carries ``node_id``, ``public_key``,
    ``status``, and its effective scopes (sorted), so any status/scope/key change
    changes the digest.
    """

    members = sorted(
        (
            {
                "node_id": member.node_id,
                "public_key": member.public_key,
                "status": member.status,
                "scopes": sorted(member.effective_scopes()),
            }
            for member in view.members
            if member.status in _ROSTER_STATUSES
        ),
        key=lambda entry: entry["node_id"],
    )
    payload = {
        "protocol_version": view.protocol_version,
        "epoch": view.epoch,
        "members": members,
    }
    digest = hashlib.sha256()
    digest.update(_CANDIDATE_VIEW_DOMAIN)
    digest.update(_canonical_json_bytes(payload))
    return digest.hexdigest()


def projected_promotion_view(view: MembershipView, node_id: str) -> MembershipView:
    """Return the view that WOULD result from promoting ``node_id`` PENDING->ACTIVE.

    Pure (applies nothing).  Used so the target can compute the exact e+2 view to
    digest and compare against the source-signed digest BEFORE self-activating,
    and so both sides digest an identical view.  ``epoch`` advances by exactly 1.
    Raises ``ValueError`` if ``node_id`` is not currently a PENDING member.
    """

    target = next((m for m in view.members if m.node_id == node_id), None)
    if target is None or target.status != MemberStatus.PENDING.value:
        raise ValueError(f"cannot project promotion: {node_id!r} is not PENDING")
    next_members = [
        (
            member.model_copy(update={"status": MemberStatus.ACTIVE.value})
            if member.node_id == node_id
            else member
        )
        for member in view.members
    ]
    return MembershipView(epoch=view.epoch + 1, members=next_members)


def roster_fully_acked(acked_node_ids: set[str], roster: set[str]) -> bool:
    """True iff every roster member has acked (set-identity, no quorum).

    A missing roster member is never success — the caller treats an incomplete
    roster ACK as ``blocked_recovery``, per PROJECT_MESH.md §7.  For the V1
    two-node bootstrap the roster is the single source, so its own ACK is the
    whole full-mesh all-ACK.
    """

    return bool(roster) and roster.issubset(acked_node_ids)


__all__ = [
    "candidate_view_digest",
    "projected_promotion_view",
    "roster_fully_acked",
]
