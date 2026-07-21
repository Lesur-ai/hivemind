# -*- coding: utf-8 -*-
"""Ordinary-write reservation guard (P10-3, issue #191).

A Project Mesh pairing TARGET is a **blank** space (no ``_hivemind/node.json``)
that must be reserved against ordinary writes while pairing runs.  A blank space
routes to ``DIRECT_LOCAL`` (not a Hivemind write route), so the STAGED/REFUSE
sink gates do NOT protect it — the guard must be threaded into each ordinary
writer instead (see ``docs``/PLAN M4 map).

This module keeps the ``core`` layer decoupled from ``mesh``: the mesh layer
registers a checker at startup (only when Mesh is enabled).  When no checker is
registered — the default, and every non-Mesh deployment — ``assert_space_not_
reserved`` is a **zero-cost no-op**, preserving byte/behaviour compatibility for
non-Mesh use.  The checker itself short-circuits in-memory and fails **closed
scoped to the reserved space only** (a mesh-store blip never wedges writes to
unrelated spaces).
"""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

#: A checker raises to REFUSE a write to a reserved space, else returns.
ReservationChecker = Callable[[str], Awaitable[None]]

_checker: Optional[ReservationChecker] = None


class SpaceReservedError(RuntimeError):
    """Raised when an ordinary write targets a space reserved for pairing."""

    def __init__(self, space_id: str) -> None:
        super().__init__(
            f"space {space_id!r} is reserved for a Project Mesh pairing and "
            "rejects ordinary writes until the pairing completes or is released"
        )
        self.space_id = space_id


def register_reservation_checker(checker: ReservationChecker) -> None:
    """Install the Mesh reservation checker (called by ``create_app`` when enabled)."""

    global _checker
    _checker = checker


def clear_reservation_checker() -> None:
    """Remove any installed checker (Mesh disabled / teardown / tests)."""

    global _checker
    _checker = None


def has_reservation_checker() -> bool:
    return _checker is not None


async def assert_space_not_reserved(space_id: str) -> None:
    """Refuse an ordinary write to a reserved space; no-op when Mesh is disabled.

    Raises the checker's own refusal (surfaced as a safe error by the tools) when
    ``space_id`` is reserved.  When no checker is registered this returns
    immediately without any I/O.
    """

    checker = _checker
    if checker is None:
        return
    await checker(space_id)


# ---------------------------------------------------------------------------
# Pairing-activation fence (P10-3): while a SOURCE pairing is between its e+1->e+2
# promotion and confirmed target activation, an OPERATOR epoch-advancing
# membership mutation (re-scope, add_member) would advance the source past e+2
# while the target self-promotes to the pre-computed e+2 — splitting the two
# MembershipViews.  The membership layer calls the fence (under its own lock)
# before such a mutation; the mesh layer registers a checker that refuses while a
# source session is mid-activation.  Same decoupling + zero-cost-no-op discipline
# as the reservation guard above.
# ---------------------------------------------------------------------------

#: An activation checker raises to REFUSE while a source pairing (OTHER than the
#: optional ``ignore_pair_id`` the caller passes for its OWN transition) is
#: mid-activation, else returns.
PairingActivationChecker = Callable[[str, Optional[str]], Awaitable[None]]

_activation_checker: Optional[PairingActivationChecker] = None


class PairingActivationError(RuntimeError):
    """Raised when an operator membership mutation would race a pairing's
    promotion-to-confirmed-activation window (would split source/target epochs)."""

    def __init__(self, space_id: str) -> None:
        super().__init__(
            f"space {space_id!r} has a Project Mesh pairing mid-activation and "
            "rejects epoch-advancing membership mutations until it converges or is "
            "given up"
        )
        self.space_id = space_id


def register_pairing_activation_checker(checker: PairingActivationChecker) -> None:
    global _activation_checker
    _activation_checker = checker


def clear_pairing_activation_checker() -> None:
    global _activation_checker
    _activation_checker = None


async def assert_no_pairing_activation(
    space_id: str, *, ignore_pair_id: Optional[str] = None
) -> None:
    """Refuse an epoch-advancing membership mutation while a source pairing for
    ``space_id`` is mid-activation; no-op when Mesh is disabled (no checker).

    ``ignore_pair_id`` is the pairing-scoped bypass: a pairing driving its OWN
    activation/give-up transition passes its ``pair_id`` so the checker ignores
    its own session (else it would self-block) while STILL refusing when a
    DIFFERENT pairing for the space is mid-activation. Operator mutations
    (add_member, update_member_scopes, apply_membership_plan, unsafe backup
    recovery) pass ``None`` and are fenced against every mid-activation pairing.
    """

    checker = _activation_checker
    if checker is None:
        return
    await checker(space_id, ignore_pair_id)


# ---------------------------------------------------------------------------
# Mesh-leader gate for out-of-band membership recovery (P10-3, cross-process):
# the pairing-activation fence above reads the DURABLE pairing store, so it sees
# the leader's sessions from any process — but the check-then-write to
# ``members.json`` is not atomic across processes, and the membership
# ``_space_lock`` is an in-process asyncio lock. A non-leader worker's unsafe
# ``backup_restore`` could pass the fence, then race the LEADER's pairing promote
# into a same-epoch roster overwrite (an undetectable split). Mesh membership is a
# single-writer authority (the flock-elected leader; Mesh routes/admin already
# reject non-leaders), so out-of-band membership recovery must ALSO run only on
# the leader — then the leader's in-process lock serializes it against promotes.
# Same registered-checker + zero-cost-no-op discipline as the guards above.
# ---------------------------------------------------------------------------

#: A leader checker raises to REFUSE a membership-recovery write on a non-leader.
MembershipRecoveryLeaderChecker = Callable[[str], Awaitable[None]]

_recovery_leader_checker: Optional[MembershipRecoveryLeaderChecker] = None


class NotMembershipLeaderError(RuntimeError):
    """Raised when out-of-band membership recovery runs on a non-leader process
    while Mesh is enabled (it must run on the single flock-elected leader so its
    roster write is serialized against pairing promotions)."""

    def __init__(self, space_id: str) -> None:
        super().__init__(
            f"membership recovery for space {space_id!r} must run on the Project "
            "Mesh leader process; this process does not hold the Mesh leader lock"
        )
        self.space_id = space_id


def register_membership_recovery_leader_checker(
    checker: MembershipRecoveryLeaderChecker,
) -> None:
    global _recovery_leader_checker
    _recovery_leader_checker = checker


def clear_membership_recovery_leader_checker() -> None:
    global _recovery_leader_checker
    _recovery_leader_checker = None


async def assert_membership_recovery_leader(space_id: str) -> None:
    """Refuse an out-of-band membership-recovery write (unsafe ``backup_restore``)
    on a non-leader process; no-op when Mesh is disabled (no checker registered)."""

    checker = _recovery_leader_checker
    if checker is None:
        return
    await checker(space_id)


__all__ = [
    "ReservationChecker",
    "PairingActivationChecker",
    "MembershipRecoveryLeaderChecker",
    "SpaceReservedError",
    "PairingActivationError",
    "NotMembershipLeaderError",
    "assert_space_not_reserved",
    "assert_no_pairing_activation",
    "assert_membership_recovery_leader",
    "clear_reservation_checker",
    "clear_pairing_activation_checker",
    "clear_membership_recovery_leader_checker",
    "has_reservation_checker",
    "register_reservation_checker",
    "register_pairing_activation_checker",
    "register_membership_recovery_leader_checker",
]
