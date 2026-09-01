# -*- coding: utf-8 -*-
"""Ordinary-write reservation guard (P10-3, issue #191).

A Project Mesh pairing TARGET is a **blank** space (no ``_hivemind/node.json``)
that must be reserved against ordinary writes while pairing runs. Existing-space
SOURCE preparation uses the same guard while its durable PREPARING intent is
active. Both begin on the ``DIRECT_LOCAL`` route, so the STAGED/REFUSE sink gates
alone cannot protect the transition — the guard is threaded into each ordinary
writer as well (see the Project Mesh plans).

This module keeps the ``core`` layer decoupled from ``mesh``. Application
startup always registers lightweight core-only preparation/provenance readers,
even when Mesh is disabled after a restart; enabled mode replaces them with the
strict Mesh store (including target reservations). Direct module consumers with
no registered checker retain the zero-cost no-op. Target-reservation misses may
short-circuit from a hydrated in-memory index. Source-preparation intent is
instead read durably on every check so a different process/config cannot hide
it; unreadable/ambiguous Mesh state fails closed and can therefore refuse beyond
one space until the local store/process is recovered.

Completed source preparation is deliberately not an ordinary-write
reservation: healthy shared writes must continue through ``STAGED``. It is,
however, irreversible provenance. A second registered checker is consulted
only after routing is about to authorize ``DIRECT_LOCAL``; it durably refuses
both PREPARING and COMPLETE evidence so loss of the Hivemind prefix cannot
downgrade a former source into a local writer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Protocol


_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)
_SOURCE_PREPARATION_PREFIX = "_system/mesh_source_preparations/"
_PREPARATION_ID_RE = re.compile(r"^prep_[0-9a-f]{32}$", re.ASCII)
_FINGERPRINT_RE = re.compile(r"^hm1:[0-9a-f]{64}$", re.ASCII)
_MEMBERSHIP_PUBLIC_KEY_RE = re.compile(
    r"^ed25519:[A-Za-z0-9_-]{43}$", re.ASCII
)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SOURCE_PREPARATION_FIELDS = frozenset(
    {
        "completed_at_ms",
        "display_name",
        "expected_state_token",
        "membership_public_key",
        "node_id",
        "preparation_id",
        "protocol_version",
        "public_url",
        "source_fingerprint",
        "space_id",
        "started_at_iso",
        "started_at_ms",
        "state",
    }
)
_MESH_FINGERPRINT_DOMAIN = b"hivemind-mesh-identity-v1\0"
_MAX_SAFE_INTEGER = (1 << 53) - 1

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
# Irreversible DIRECT_LOCAL provenance fence (#413). Source-preparation
# evidence outlives the Hivemind prefix it initialized. If that prefix is
# accidentally deleted/restored empty, routing alone sees a local space. Mesh
# registers a durable checker that refuses DIRECT_LOCAL whenever ANY intent
# exists. This slot is intentionally separate from the reservation checker:
# COMPLETE must not block legitimate STAGED writes.
# ---------------------------------------------------------------------------

DirectLocalChecker = Callable[[str], Awaitable[None]]

_direct_local_checker: Optional[DirectLocalChecker] = None


class ProvenanceStorage(Protocol):
    async def get(self, key: str) -> str | None: ...


class DirectLocalProvenanceError(RuntimeError):
    """Bounded refusal when durable source provenance exists or is unreadable."""

    def __init__(self, space_id: str) -> None:
        super().__init__(
            f"direct-local access for space {space_id!r} is refused because "
            "Project Mesh source provenance exists or cannot be verified"
        )
        self.space_id = space_id


def source_preparation_key(space_id: str) -> str:
    """Return the stable fingerprint-neutral provenance key."""

    if type(space_id) is not str or _SPACE_ID_RE.fullmatch(space_id) is None:
        raise ValueError("invalid source preparation space id")
    return f"{_SOURCE_PREPARATION_PREFIX}{space_id}.json"


async def assert_no_source_preparation_provenance(
    storage: ProvenanceStorage, space_id: str
) -> None:
    """Durably refuse any source intent without importing the Mesh package.

    This is the disabled-Mesh fallback. It deliberately performs a fresh GET
    on every prospective DIRECT_LOCAL authority: a negative cache could hide
    an intent written by another process. Any backend ambiguity fails closed.
    """

    try:
        evidence = await storage.get(source_preparation_key(space_id))
    except Exception as exc:
        raise DirectLocalProvenanceError(space_id) from exc
    if evidence is not None:
        raise DirectLocalProvenanceError(space_id)


async def assert_no_active_source_preparation(
    storage: ProvenanceStorage, space_id: str
) -> None:
    """Core-only PREPARING reservation check used even when Mesh is disabled.

    COMPLETE is the only evidence that releases the temporary ordinary-write
    reservation.  Since disabled mode intentionally cannot import
    :mod:`live_mem.mesh`, validate the complete public record here using only
    standard-library primitives.  A state-only overwrite must never turn a
    PREPARING crash fence into an apparently completed transition.
    """

    try:
        evidence = await storage.get(source_preparation_key(space_id))
        if evidence is None:
            return
        decoded = _parse_core_source_preparation(evidence, space_id)
    except Exception as exc:
        raise SpaceReservedError(space_id) from exc
    if decoded["state"] == "complete":
        return
    raise SpaceReservedError(space_id)


def _parse_core_source_preparation(evidence: object, space_id: str) -> dict:
    """Parse the exact canonical public preparation record without Mesh imports."""

    if type(evidence) is not str:
        raise ValueError("invalid source preparation evidence")
    raw = evidence.encode("utf-8", "strict")
    if len(raw) > 65_536:
        raise ValueError("source preparation evidence is too large")

    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate source preparation field")
            result[key] = value
        return result

    def parse_int(token: str) -> int:
        value = int(token, 10)
        if not -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER:
            raise ValueError("source preparation integer is out of range")
        return value

    def reject_number(_token: str):
        raise ValueError("source preparation numbers must be integers")

    decoded = json.loads(
        evidence,
        object_pairs_hook=object_pairs,
        parse_int=parse_int,
        parse_float=reject_number,
        parse_constant=reject_number,
    )
    if type(decoded) is not dict or set(decoded) != _SOURCE_PREPARATION_FIELDS:
        raise ValueError("source preparation fields are invalid")
    canonical = json.dumps(
        decoded,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", "strict")
    if canonical != raw:
        raise ValueError("source preparation evidence is not canonical")

    preparation_id = decoded["preparation_id"]
    fingerprint = decoded["source_fingerprint"]
    membership_key = decoded["membership_public_key"]
    node_id = decoded["node_id"]
    state = decoded["state"]
    if (
        type(preparation_id) is not str
        or _PREPARATION_ID_RE.fullmatch(preparation_id) is None
        or type(decoded["protocol_version"]) is not int
        or decoded["protocol_version"] != 1
        or type(state) is not str
        or state not in {"preparing", "complete"}
        or type(decoded["space_id"]) is not str
        or decoded["space_id"] != space_id
        or _SPACE_ID_RE.fullmatch(decoded["space_id"]) is None
        or type(fingerprint) is not str
        or _FINGERPRINT_RE.fullmatch(fingerprint) is None
        or type(membership_key) is not str
        or _MEMBERSHIP_PUBLIC_KEY_RE.fullmatch(membership_key) is None
        or type(node_id) is not str
        or node_id != fingerprint.split(":", 1)[1]
    ):
        raise ValueError("source preparation identity is invalid")

    key_payload = membership_key.split(":", 1)[1]
    key_bytes = base64.b64decode(
        key_payload + "=", altchars=b"-_", validate=True
    )
    if (
        len(key_bytes) != 32
        or base64.urlsafe_b64encode(key_bytes).decode("ascii").rstrip("=")
        != key_payload
        or "hm1:"
        + hashlib.sha256(_MESH_FINGERPRINT_DOMAIN + key_bytes).hexdigest()
        != fingerprint
    ):
        raise ValueError("source preparation key binding is invalid")

    display_name = decoded["display_name"]
    if type(display_name) is not str:
        raise ValueError("source preparation display name is invalid")
    display_bytes = display_name.encode("utf-8", "strict")
    if (
        not display_name
        or len(display_bytes) > 128
        or unicodedata.normalize("NFC", display_name) != display_name
        or any(
            unicodedata.category(char).startswith("C")
            or char in {"\u2028", "\u2029"}
            for char in display_name
        )
    ):
        raise ValueError("source preparation display name is invalid")

    public_url = decoded["public_url"]
    expected_token = decoded["expected_state_token"]
    started_ms = decoded["started_at_ms"]
    started_iso = decoded["started_at_iso"]
    completed_ms = decoded["completed_at_ms"]
    if (
        type(public_url) is not str
        or not public_url.startswith("https://")
        or len(public_url) > 2048
        or type(expected_token) is not str
        or _DIGEST_RE.fullmatch(expected_token) is None
        or type(started_ms) is not int
        or started_ms < 0
        or type(started_iso) is not str
        or not started_iso
        or len(started_iso) > 64
        or type(completed_ms) is not int
        or completed_ms < 0
    ):
        raise ValueError("source preparation metadata is invalid")
    try:
        parsed_start = datetime.fromisoformat(started_iso.replace("Z", "+00:00"))
        timestamps_match = (
            parsed_start.tzinfo is not None
            and parsed_start.utcoffset() == timezone.utc.utcoffset(parsed_start)
            and parsed_start.microsecond % 1000 == 0
            and int(parsed_start.timestamp() * 1000) == started_ms
        )
    except (ValueError, OverflowError, OSError):
        timestamps_match = False
    if not timestamps_match:
        raise ValueError("source preparation timestamps are invalid")
    if state == "preparing":
        if completed_ms != 0:
            raise ValueError("preparing source preparation is invalid")
    elif completed_ms <= 0 or completed_ms < started_ms:
        raise ValueError("completed source preparation is invalid")
    return decoded


def register_direct_local_checker(checker: DirectLocalChecker) -> None:
    """Install the Mesh irreversible-provenance checker."""

    global _direct_local_checker
    _direct_local_checker = checker


def clear_direct_local_checker() -> None:
    """Remove the checker (Mesh disabled / teardown / tests)."""

    global _direct_local_checker
    _direct_local_checker = None


def has_direct_local_checker() -> bool:
    return _direct_local_checker is not None


async def assert_direct_local_allowed(space_id: str) -> None:
    """Refuse prospective DIRECT_LOCAL authority with durable Mesh provenance.

    Callers invoke this only after the current route resolves DIRECT_LOCAL.
    With no checker registered it is a zero-I/O no-op. Server startup keeps a
    core-only durable checker active even when Mesh is disabled.
    """

    checker = _direct_local_checker
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


@dataclass(frozen=True, slots=True)
class ReservationGuardSnapshot:
    """Opaque process-global checker snapshot used by test isolation only."""

    reservation: Optional[ReservationChecker]
    direct_local: Optional[DirectLocalChecker]
    activation: Optional[PairingActivationChecker]
    recovery_leader: Optional[MembershipRecoveryLeaderChecker]


def snapshot_for_tests() -> ReservationGuardSnapshot:
    """Capture all checker slots without changing production lifecycle state."""

    return ReservationGuardSnapshot(
        reservation=_checker,
        direct_local=_direct_local_checker,
        activation=_activation_checker,
        recovery_leader=_recovery_leader_checker,
    )


def restore_for_tests(snapshot: ReservationGuardSnapshot) -> None:
    """Restore one exact snapshot; intended for the suite autouse fixture."""

    if not isinstance(snapshot, ReservationGuardSnapshot):
        raise TypeError("invalid reservation guard test snapshot")
    global _checker, _direct_local_checker, _activation_checker
    global _recovery_leader_checker
    _checker = snapshot.reservation
    _direct_local_checker = snapshot.direct_local
    _activation_checker = snapshot.activation
    _recovery_leader_checker = snapshot.recovery_leader


__all__ = [
    "ReservationChecker",
    "DirectLocalChecker",
    "ProvenanceStorage",
    "PairingActivationChecker",
    "MembershipRecoveryLeaderChecker",
    "ReservationGuardSnapshot",
    "SpaceReservedError",
    "PairingActivationError",
    "NotMembershipLeaderError",
    "DirectLocalProvenanceError",
    "assert_no_source_preparation_provenance",
    "assert_no_active_source_preparation",
    "assert_direct_local_allowed",
    "assert_space_not_reserved",
    "assert_no_pairing_activation",
    "assert_membership_recovery_leader",
    "clear_reservation_checker",
    "clear_direct_local_checker",
    "clear_pairing_activation_checker",
    "clear_membership_recovery_leader_checker",
    "has_reservation_checker",
    "has_direct_local_checker",
    "register_direct_local_checker",
    "register_reservation_checker",
    "register_pairing_activation_checker",
    "register_membership_recovery_leader_checker",
    "snapshot_for_tests",
    "restore_for_tests",
    "source_preparation_key",
]
