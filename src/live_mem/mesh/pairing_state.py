# -*- coding: utf-8 -*-
"""Durable Project Mesh pairing state machine and local session model (P10-3).

This module defines the **closed 10-state** pairing lifecycle frozen at P10-0
(PROJECT_MESH.md §4), the two roles (source / target), the ``MeshPairingSession``
durable local model, its legal-transition table, and the signed
``BlockedRecoveryEvidence`` that backs a fail-closed ``resume``.

A pairing session is **local operational state** — never shared memory, never a
long-memory document, never membership authority (PROJECT_MESH.md §4).  It is
persisted by :mod:`live_mem.mesh.pairing_store` under
``_system/mesh_pairing/<instance-fingerprint>/sessions/``.  The model carries
only public identifiers and domain-separated digests; the one-time invitation
secret appears only as its ``secret_digest`` and the private key never enters it.

The pre/post shared-membership-mutation boundary is between ``approved`` (the
approval is signed but Transition 1 — the ``pending`` admission at e+1 — is not
yet committed) and ``transferring`` (Transition 1 committed).  Only pre-mutation
states release the target reservation on exit; a failure in any post-mutation
state goes to ``blocked_recovery`` and never silently rolls membership back.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Mapping

from .canonical import HCJError, canonical_dumps, canonical_loads
from .identity import MeshIdentityError, MeshPrivateKey, parse_mesh_public_key

_PAIR_ID_RE = re.compile(r"^pair_[0-9a-f]{32}$", re.ASCII)
_FINGERPRINT_RE = re.compile(r"^hm1:[0-9a-f]{64}$", re.ASCII)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PUBLIC_KEY_RE = re.compile(r"^ed25519-public:v1:[A-Za-z0-9_-]{43}$", re.ASCII)
_EVENT_ID_RE = re.compile(r"^[0-9a-f-]{1,64}$", re.ASCII)
_SCOPES = ("read", "propose", "commit")
_NEXT_ACTIONS = ("resume", "resync", "evict")

#: Domain separator for the signed blocked-recovery evidence structure.
_BLOCKED_RECOVERY_DOMAIN = b"hivemind-mesh-blocked-recovery-v1\0"
_PROTOCOL_VERSION = 1


class MeshPairingError(ValueError):
    """A non-reflective pairing-model refusal."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class MeshPairingState(str, Enum):
    """The closed 10-state Project Mesh pairing lifecycle (PROJECT_MESH.md §4)."""

    ISSUED = "issued"
    CLAIMED = "claimed"
    APPROVED = "approved"
    TRANSFERRING = "transferring"
    AWAITING_ACKS = "awaiting_acks"
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REFUSED = "refused"
    BLOCKED_RECOVERY = "blocked_recovery"


class MeshPairingRole(str, Enum):
    """Which side of the interactive pairing this local session plays."""

    SOURCE = "source"  # administrator A's instance (owns the space being joined)
    TARGET = "target"  # administrator B's blank instance being enrolled


#: States before any shared membership mutation.  Exiting one of these to a
#: terminal state releases the target reservation and leaves membership
#: unchanged (PROJECT_MESH.md §7).
PRE_MUTATION_STATES: frozenset[MeshPairingState] = frozenset(
    {MeshPairingState.ISSUED, MeshPairingState.CLAIMED, MeshPairingState.APPROVED}
)
#: Terminal states (no outgoing transition).
TERMINAL_STATES: frozenset[MeshPairingState] = frozenset(
    {
        MeshPairingState.ACTIVE,
        MeshPairingState.EXPIRED,
        MeshPairingState.CANCELLED,
        MeshPairingState.REFUSED,
    }
)

# Legal transitions per role.  A transition NOT listed here is refused
# fail-closed by ``MeshPairingSession.transition``.
_S = MeshPairingState
_COMMON: dict[MeshPairingState, frozenset[MeshPairingState]] = {
    _S.CLAIMED: frozenset({_S.APPROVED, _S.REFUSED, _S.CANCELLED, _S.EXPIRED}),
    _S.APPROVED: frozenset({_S.TRANSFERRING, _S.CANCELLED, _S.REFUSED, _S.EXPIRED}),
    _S.TRANSFERRING: frozenset({_S.AWAITING_ACKS, _S.BLOCKED_RECOVERY}),
    _S.AWAITING_ACKS: frozenset({_S.ACTIVE, _S.BLOCKED_RECOVERY}),
    # blocked_recovery -> active (resume/activation succeeds) or cancelled
    # (explicit eviction abandons the candidate through the membership authority).
    _S.BLOCKED_RECOVERY: frozenset({_S.ACTIVE, _S.CANCELLED}),
    _S.ACTIVE: frozenset(),
    _S.EXPIRED: frozenset(),
    _S.CANCELLED: frozenset(),
    _S.REFUSED: frozenset(),
}
_SOURCE_TRANSITIONS: dict[MeshPairingState, frozenset[MeshPairingState]] = {
    _S.ISSUED: frozenset({_S.CLAIMED, _S.EXPIRED, _S.CANCELLED}),
    **_COMMON,
}
# The target never issues an invitation; its session starts at ``claimed``.  It
# additionally permits ``blocked_recovery -> transferring`` so a corrupt-import
# ``resync`` can tear the target back to blank and re-drive the SAME e+1 transfer
# after verifying its signed evidence (the source keeps only the resume/evict
# exits, so this edge is target-only).
_TARGET_TRANSITIONS: dict[MeshPairingState, frozenset[MeshPairingState]] = dict(_COMMON)
_TARGET_TRANSITIONS[_S.BLOCKED_RECOVERY] = _COMMON[_S.BLOCKED_RECOVERY] | frozenset({_S.TRANSFERRING})

_TRANSITIONS: dict[MeshPairingRole, dict[MeshPairingState, frozenset[MeshPairingState]]] = {
    MeshPairingRole.SOURCE: _SOURCE_TRANSITIONS,
    MeshPairingRole.TARGET: _TARGET_TRANSITIONS,
}


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise MeshPairingError(code, message)


def _check_str(value: Any, *, allow_empty: bool = False) -> str:
    _require(type(value) is str, "invalid_field", "pairing field must be a string")
    _require(allow_empty or bool(value), "empty_field", "pairing field must be non-empty")
    return value


def _check_digest(value: Any, *, allow_empty: bool = True) -> str:
    value = _check_str(value, allow_empty=allow_empty)
    _require(
        value == "" or _DIGEST_RE.fullmatch(value) is not None,
        "invalid_digest",
        "pairing digest must be lowercase 64-hex",
    )
    return value


@dataclass(frozen=True, slots=True)
class MeshPairingSession:
    """A durable local pairing session (one per pair id per instance).

    Every field is present at all times; ``""`` / ``-1`` are the not-yet-known
    sentinels.  Instances are immutable — advance the lifecycle via
    :meth:`transition`, which validates the (role, from -> to) edge.
    """

    pair_id: str
    role: str
    state: str
    space_id: str
    protocol_version: int
    source_public_key: str
    source_fingerprint: str
    source_endpoint: str
    target_public_key: str
    target_fingerprint: str
    target_endpoint: str
    granted_scopes: tuple[str, ...]
    base_epoch: int
    invitation_digest: str
    secret_digest: str
    claim_digest: str
    approval_digest: str
    bootstrap_manifest_digest: str
    bootstrap_bank_version: int
    activation_event_id: str
    last_error: str
    created_at_ms: int
    updated_at_ms: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        _require(_PAIR_ID_RE.fullmatch(self.pair_id) is not None, "invalid_pair_id", "invalid pair id")
        _require(self.role in (r.value for r in MeshPairingRole), "invalid_role", "invalid role")
        _require(self.state in (s.value for s in MeshPairingState), "invalid_state", "invalid state")
        _check_str(self.space_id)
        _require(self.protocol_version == _PROTOCOL_VERSION, "invalid_version", "unsupported version")
        _require(
            _PUBLIC_KEY_RE.fullmatch(self.source_public_key) is not None,
            "invalid_source_key",
            "invalid source public key",
        )
        _require(
            _FINGERPRINT_RE.fullmatch(self.source_fingerprint) is not None,
            "invalid_source_fp",
            "invalid source fingerprint",
        )
        _check_str(self.source_endpoint)
        for key, allow in (
            (self.target_public_key, _PUBLIC_KEY_RE),
            (self.target_fingerprint, _FINGERPRINT_RE),
        ):
            _require(
                key == "" or allow.fullmatch(key) is not None,
                "invalid_target_identity",
                "invalid target identity",
            )
        _check_str(self.target_endpoint, allow_empty=True)
        _require(type(self.granted_scopes) is tuple, "invalid_scopes", "scopes must be a tuple")
        _require(
            all(s in _SCOPES for s in self.granted_scopes)
            and list(self.granted_scopes) == sorted(set(self.granted_scopes))
            and "read" in self.granted_scopes,
            "invalid_scopes",
            "granted scopes must be a sorted subset of {read,propose,commit} including read",
        )
        _require(type(self.base_epoch) is int and self.base_epoch >= 0, "invalid_epoch", "invalid base epoch")
        _check_digest(self.invitation_digest, allow_empty=True)
        _check_digest(self.secret_digest, allow_empty=True)
        _check_digest(self.claim_digest, allow_empty=True)
        _check_digest(self.approval_digest, allow_empty=True)
        _check_digest(self.bootstrap_manifest_digest, allow_empty=True)
        _require(
            type(self.bootstrap_bank_version) is int and self.bootstrap_bank_version >= -1,
            "invalid_bank_version",
            "invalid bootstrap bank version",
        )
        _require(
            self.activation_event_id == "" or _EVENT_ID_RE.fullmatch(self.activation_event_id) is not None,
            "invalid_event_id",
            "invalid activation event id",
        )
        _check_str(self.last_error, allow_empty=True)
        for ts in (self.created_at_ms, self.updated_at_ms, self.expires_at_ms):
            _require(type(ts) is int and ts >= 0, "invalid_timestamp", "invalid timestamp")

    # ---- lifecycle -------------------------------------------------------

    @property
    def state_enum(self) -> MeshPairingState:
        return MeshPairingState(self.state)

    @property
    def role_enum(self) -> MeshPairingRole:
        return MeshPairingRole(self.role)

    def is_pre_mutation(self) -> bool:
        return self.state_enum in PRE_MUTATION_STATES

    def is_terminal(self) -> bool:
        return self.state_enum in TERMINAL_STATES

    def is_expired(self, now_ms: int) -> bool:
        """True when a PRE-MUTATION session has passed its one-time lifetime."""

        return self.is_pre_mutation() and now_ms >= self.expires_at_ms

    def transition(self, to_state: MeshPairingState, *, now_ms: int, **updates: Any) -> "MeshPairingSession":
        """Return a new session at ``to_state`` if the edge is legal, else raise.

        The (role, current -> to_state) edge must be in the frozen transition
        table; an illegal transition is a fail-closed :class:`MeshPairingError`,
        never a silent no-op.  ``updates`` overwrite fields; ``updated_at_ms`` is
        always refreshed and the result is re-validated by ``__post_init__``.
        """

        current = self.state_enum
        allowed = _TRANSITIONS[self.role_enum].get(current, frozenset())
        _require(
            to_state in allowed,
            "illegal_transition",
            f"illegal pairing transition {current.value} -> {to_state.value}",
        )
        return replace(self, state=to_state.value, updated_at_ms=now_ms, **updates)

    def with_fields(self, *, now_ms: int, **updates: Any) -> "MeshPairingSession":
        """Return a copy with ``updates`` applied at the SAME state (no edge)."""

        return replace(self, updated_at_ms=now_ms, **updates)

    # ---- serialization (HCJ canonical) -----------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "activation_event_id": self.activation_event_id,
            "approval_digest": self.approval_digest,
            "base_epoch": self.base_epoch,
            "bootstrap_bank_version": self.bootstrap_bank_version,
            "bootstrap_manifest_digest": self.bootstrap_manifest_digest,
            "claim_digest": self.claim_digest,
            "created_at_ms": self.created_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "granted_scopes": list(self.granted_scopes),
            "invitation_digest": self.invitation_digest,
            "last_error": self.last_error,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "role": self.role,
            "secret_digest": self.secret_digest,
            "source_endpoint": self.source_endpoint,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "state": self.state,
            "target_endpoint": self.target_endpoint,
            "target_fingerprint": self.target_fingerprint,
            "target_public_key": self.target_public_key,
            "updated_at_ms": self.updated_at_ms,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MeshPairingSession":
        _require(isinstance(data, Mapping), "invalid_session", "session must be a mapping")
        scopes = data.get("granted_scopes")
        _require(type(scopes) is list, "invalid_scopes", "granted_scopes must be a list")
        try:
            return cls(
                pair_id=data["pair_id"],
                role=data["role"],
                state=data["state"],
                space_id=data["space_id"],
                protocol_version=data["protocol_version"],
                source_public_key=data["source_public_key"],
                source_fingerprint=data["source_fingerprint"],
                source_endpoint=data["source_endpoint"],
                target_public_key=data["target_public_key"],
                target_fingerprint=data["target_fingerprint"],
                target_endpoint=data["target_endpoint"],
                granted_scopes=tuple(scopes),
                base_epoch=data["base_epoch"],
                invitation_digest=data["invitation_digest"],
                secret_digest=data["secret_digest"],
                claim_digest=data["claim_digest"],
                approval_digest=data["approval_digest"],
                bootstrap_manifest_digest=data["bootstrap_manifest_digest"],
                bootstrap_bank_version=data["bootstrap_bank_version"],
                activation_event_id=data["activation_event_id"],
                last_error=data["last_error"],
                created_at_ms=data["created_at_ms"],
                updated_at_ms=data["updated_at_ms"],
                expires_at_ms=data["expires_at_ms"],
            )
        except KeyError as exc:
            raise MeshPairingError("missing_field", "pairing session is missing a field") from exc

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MeshPairingSession":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class BlockedRecoveryEvidence:
    """Signed, canonical, domain-separated evidence for a blocked recovery.

    Carries ONLY public identifiers / digests / epoch — never a secret, private
    key, snapshot, or note content.  Signed by the local instance Ed25519 key so
    ``resume`` can verify it (against the local public key) before acting, and so
    a corrupted-but-parseable persisted session cannot drive an unauthenticated
    recovery action.
    """

    pair_id: str
    space_id: str
    epoch: int
    phase: str
    next_action: str
    manifest_digest: str
    candidate_view_digest: str
    activation_event_id: str
    issued_at_ms: int

    def __post_init__(self) -> None:
        _require(_PAIR_ID_RE.fullmatch(self.pair_id) is not None, "invalid_pair_id", "invalid pair id")
        _check_str(self.space_id)
        _require(type(self.epoch) is int and self.epoch >= 0, "invalid_epoch", "invalid epoch")
        _check_str(self.phase)
        _require(self.next_action in _NEXT_ACTIONS, "invalid_next_action", "invalid next action")
        _check_digest(self.manifest_digest, allow_empty=True)
        _check_digest(self.candidate_view_digest, allow_empty=True)
        _require(
            self.activation_event_id == "" or _EVENT_ID_RE.fullmatch(self.activation_event_id) is not None,
            "invalid_event_id",
            "invalid activation event id",
        )
        _require(type(self.issued_at_ms) is int and self.issued_at_ms >= 0, "invalid_timestamp", "invalid timestamp")

    def as_dict(self) -> dict[str, Any]:
        return {
            "activation_event_id": self.activation_event_id,
            "candidate_view_digest": self.candidate_view_digest,
            "epoch": self.epoch,
            "issued_at_ms": self.issued_at_ms,
            "manifest_digest": self.manifest_digest,
            "next_action": self.next_action,
            "pair_id": self.pair_id,
            "phase": self.phase,
            "space_id": self.space_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BlockedRecoveryEvidence":
        _require(isinstance(data, Mapping), "invalid_evidence", "evidence must be a mapping")
        try:
            return cls(
                pair_id=data["pair_id"],
                space_id=data["space_id"],
                epoch=data["epoch"],
                phase=data["phase"],
                next_action=data["next_action"],
                manifest_digest=data["manifest_digest"],
                candidate_view_digest=data["candidate_view_digest"],
                activation_event_id=data["activation_event_id"],
                issued_at_ms=data["issued_at_ms"],
            )
        except KeyError as exc:
            raise MeshPairingError("missing_field", "evidence is missing a field") from exc


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str, *, expected_len: int) -> bytes:
    if type(value) is not str:
        raise MeshPairingError("invalid_signature", "signature must be a string")
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise MeshPairingError("invalid_signature", "signature is not valid base64url") from exc
    if len(raw) != expected_len:
        raise MeshPairingError("invalid_signature", "signature has the wrong length")
    return raw


@dataclass(frozen=True, slots=True)
class SignedBlockedRecoveryEvidence:
    """A ``BlockedRecoveryEvidence`` plus its detached local-instance signature."""

    evidence: BlockedRecoveryEvidence
    signature: str  # canonical unpadded base64url of 64 Ed25519 bytes

    @classmethod
    def sign(
        cls, evidence: BlockedRecoveryEvidence, private_key: MeshPrivateKey
    ) -> "SignedBlockedRecoveryEvidence":
        signature = private_key.sign(_BLOCKED_RECOVERY_DOMAIN + evidence.canonical_bytes())
        return cls(evidence=evidence, signature=_b64url_no_pad(signature))

    def verify(self, public_key: str) -> None:
        """Raise :class:`MeshPairingError` unless the signature is valid.

        ``public_key`` must be the local instance's Mesh public key that signed
        the evidence.
        """

        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError("invalid_key", "invalid evidence signer key") from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(raw, _BLOCKED_RECOVERY_DOMAIN + self.evidence.canonical_bytes())
        except Exception as exc:  # cryptography raises InvalidSignature
            raise MeshPairingError("bad_signature", "blocked-recovery evidence signature is invalid") from exc

    def as_dict(self) -> dict[str, Any]:
        return {"evidence": self.evidence.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedBlockedRecoveryEvidence":
        data = canonical_loads(raw)
        if not isinstance(data, Mapping) or "evidence" not in data or "signature" not in data:
            raise MeshPairingError("invalid_evidence", "signed evidence is malformed")
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError("invalid_signature", "signature must be a string")
        return cls(
            evidence=BlockedRecoveryEvidence.from_dict(data["evidence"]),
            signature=signature,
        )


__all__ = [
    "MeshPairingError",
    "MeshPairingState",
    "MeshPairingRole",
    "MeshPairingSession",
    "BlockedRecoveryEvidence",
    "SignedBlockedRecoveryEvidence",
    "PRE_MUTATION_STATES",
    "TERMINAL_STATES",
]
