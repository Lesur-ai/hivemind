# -*- coding: utf-8 -*-
"""Signed Project Mesh enrollment-intent artifacts.

The invitation, join claim, and enrollment approval form the declared intent
triple from ADR-0024.  They are deliberately separate from HTTP operations and
from membership application.  This module only validates immutable bytes and
performs the pure T17 approval-authority read check.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Final, TypeAlias

from cryptography.exceptions import InvalidSignature

from ..core.hivemind.models import MemberStatus, MembershipView, PeerScope
from .canonical import HCJError, JSONValue, canonical_dumps, canonical_loads
from .identity import (
    MeshIdentityError,
    MeshPrivateKey,
    decode_membership_public_key,
    decode_mesh_public_key,
    mesh_identity_fingerprint,
    parse_mesh_public_key,
)


MESH_ARTIFACT_PROTOCOL_VERSION: Final = 1
MESH_TARGET_UNBOUND: Final = "mesh-target-unbound-v1"
MESH_INVITATION_TTL_MILLISECONDS: Final = 3_600_000

INVITATION_SIGNATURE_DOMAIN: Final = b"hivemind-mesh-invitation-v1\0"
JOIN_CLAIM_SIGNATURE_DOMAIN: Final = b"hivemind-mesh-join-claim-v1\0"
ENROLLMENT_APPROVAL_SIGNATURE_DOMAIN: Final = (
    b"hivemind-mesh-enrollment-approval-v1\0"
)
_PAIR_ID_RE = re.compile(r"^pair_[0-9a-f]{32}$", re.ASCII)
_NONCE_RE = re.compile(r"^nonce_[0-9a-f]{64}$", re.ASCII)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_FINGERPRINT_RE = re.compile(r"^hm1:[0-9a-f]{64}$", re.ASCII)
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)
_B64URL_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9_-]{86}$", re.ASCII)
_SCOPES = frozenset({"read", "propose", "commit"})


class MeshArtifactError(ValueError):
    """Machine-readable, non-reflective artifact refusal."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _fail(code: str, message: str) -> "None":
    raise MeshArtifactError(code, message)


class MeshArtifactKind(str, Enum):
    INVITATION = "invitation"
    JOIN_CLAIM = "join_claim"
    ENROLLMENT_APPROVAL = "enrollment_approval"


def _plain_string(value: object, pattern: re.Pattern[str], code: str) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(code, "Mesh artifact field is invalid")
    return value


def _nonnegative_safe_int(value: object, code: str) -> int:
    if type(value) is not int or not 0 <= value <= ((1 << 53) - 1):
        _fail(code, "Mesh artifact integer is invalid")
    return value


def _identity(public_key: object, fingerprint: object, role: str) -> tuple[str, str]:
    if type(public_key) is not str or type(fingerprint) is not str:
        _fail(f"invalid_{role}_identity", "Mesh artifact identity is invalid")
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        _fail(f"invalid_{role}_identity", "Mesh artifact identity is invalid")
    try:
        computed = mesh_identity_fingerprint(public_key)
    except (MeshIdentityError, TypeError, ValueError) as exc:
        raise MeshArtifactError(
            f"invalid_{role}_identity", "Mesh artifact identity is invalid"
        ) from exc
    if computed != fingerprint:
        _fail(f"{role}_identity_mismatch", "Mesh artifact identity is inconsistent")
    return public_key, fingerprint


def _scopes(value: object, code: str) -> tuple[str, ...]:
    if type(value) not in (list, tuple):
        _fail(code, "Mesh artifact scopes are invalid")
    if any(type(scope) is not str or scope not in _SCOPES for scope in value):
        _fail(code, "Mesh artifact scopes are invalid")
    normalized = tuple(value)
    if not normalized or len(set(normalized)) != len(normalized):
        _fail(code, "Mesh artifact scopes are invalid")
    if normalized != tuple(sorted(normalized)) or "read" not in normalized:
        _fail(code, "Mesh artifact scopes are invalid")
    return normalized


def _common(
    *,
    protocol_version: object,
    pair_id: object,
    space_id: object,
    membership_epoch: object,
    issued_at_ms: object,
    nonce: object,
    source_public_key: object,
    source_fingerprint: object,
) -> None:
    if type(protocol_version) is not int or protocol_version != 1:
        _fail("wrong_protocol_version", "Mesh artifact protocol is incompatible")
    _plain_string(pair_id, _PAIR_ID_RE, "invalid_pair_id")
    _plain_string(space_id, _SPACE_ID_RE, "invalid_space_id")
    _nonnegative_safe_int(membership_epoch, "invalid_membership_epoch")
    _nonnegative_safe_int(issued_at_ms, "invalid_issued_at")
    _plain_string(nonce, _NONCE_RE, "invalid_nonce")
    _identity(source_public_key, source_fingerprint, "source")


_INVITATION_FIELDS = frozenset(
    {
        "expires_at_ms",
        "issued_at_ms",
        "kind",
        "membership_epoch",
        "nonce",
        "pair_id",
        "protocol_version",
        "secret_digest",
        "source_fingerprint",
        "source_public_key",
        "space_id",
        "target_binding",
    }
)


@dataclass(frozen=True, slots=True)
class MeshInvitation:
    protocol_version: int
    kind: MeshArtifactKind
    pair_id: str
    space_id: str
    source_public_key: str
    source_fingerprint: str
    target_binding: str
    membership_epoch: int
    issued_at_ms: int
    expires_at_ms: int
    nonce: str
    secret_digest: str

    def __post_init__(self) -> None:
        if self.kind is not MeshArtifactKind.INVITATION:
            _fail("wrong_artifact_kind", "Mesh invitation kind is invalid")
        _common(
            protocol_version=self.protocol_version,
            pair_id=self.pair_id,
            space_id=self.space_id,
            membership_epoch=self.membership_epoch,
            issued_at_ms=self.issued_at_ms,
            nonce=self.nonce,
            source_public_key=self.source_public_key,
            source_fingerprint=self.source_fingerprint,
        )
        if type(self.target_binding) is not str or self.target_binding != MESH_TARGET_UNBOUND:
            _fail("invalid_target_binding", "Mesh invitation target binding is invalid")
        _nonnegative_safe_int(self.expires_at_ms, "invalid_expiry")
        if (
            self.expires_at_ms - self.issued_at_ms
            != MESH_INVITATION_TTL_MILLISECONDS
        ):
            _fail("invalid_expiry", "Mesh invitation expiry is invalid")
        _plain_string(self.secret_digest, _DIGEST_RE, "invalid_secret_digest")

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "expires_at_ms": self.expires_at_ms,
            "issued_at_ms": self.issued_at_ms,
            "kind": self.kind.value,
            "membership_epoch": self.membership_epoch,
            "nonce": self.nonce,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "secret_digest": self.secret_digest,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "target_binding": self.target_binding,
        }


_CLAIM_FIELDS = frozenset(
    {
        "invitation_digest",
        "issued_at_ms",
        "kind",
        "membership_epoch",
        "nonce",
        "pair_id",
        "protocol_version",
        "requested_scopes",
        "source_fingerprint",
        "source_public_key",
        "space_id",
        "target_fingerprint",
        "target_public_key",
    }
)


@dataclass(frozen=True, slots=True)
class MeshJoinClaim:
    protocol_version: int
    kind: MeshArtifactKind
    pair_id: str
    space_id: str
    source_public_key: str
    source_fingerprint: str
    target_public_key: str
    target_fingerprint: str
    membership_epoch: int
    issued_at_ms: int
    nonce: str
    invitation_digest: str
    requested_scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind is not MeshArtifactKind.JOIN_CLAIM:
            _fail("wrong_artifact_kind", "Mesh join-claim kind is invalid")
        _common(
            protocol_version=self.protocol_version,
            pair_id=self.pair_id,
            space_id=self.space_id,
            membership_epoch=self.membership_epoch,
            issued_at_ms=self.issued_at_ms,
            nonce=self.nonce,
            source_public_key=self.source_public_key,
            source_fingerprint=self.source_fingerprint,
        )
        _identity(self.target_public_key, self.target_fingerprint, "target")
        if self.target_fingerprint == MESH_TARGET_UNBOUND:
            _fail("unbound_target_forbidden", "Mesh join claim requires a target")
        if self.source_fingerprint == self.target_fingerprint:
            _fail("self_pairing_forbidden", "Mesh source and target must differ")
        _plain_string(
            self.invitation_digest, _DIGEST_RE, "invalid_invitation_digest"
        )
        object.__setattr__(
            self,
            "requested_scopes",
            _scopes(self.requested_scopes, "invalid_requested_scopes"),
        )

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "invitation_digest": self.invitation_digest,
            "issued_at_ms": self.issued_at_ms,
            "kind": self.kind.value,
            "membership_epoch": self.membership_epoch,
            "nonce": self.nonce,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "requested_scopes": list(self.requested_scopes),
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
            "target_public_key": self.target_public_key,
        }


_APPROVAL_FIELDS = frozenset(
    {
        "granted_scopes",
        "invitation_digest",
        "issued_at_ms",
        "join_claim_digest",
        "kind",
        "membership_epoch",
        "nonce",
        "pair_id",
        "protocol_version",
        "source_fingerprint",
        "source_public_key",
        "space_id",
        "target_fingerprint",
        "target_public_key",
    }
)


@dataclass(frozen=True, slots=True)
class MeshEnrollmentApproval:
    protocol_version: int
    kind: MeshArtifactKind
    pair_id: str
    space_id: str
    source_public_key: str
    source_fingerprint: str
    target_public_key: str
    target_fingerprint: str
    membership_epoch: int
    issued_at_ms: int
    nonce: str
    invitation_digest: str
    join_claim_digest: str
    granted_scopes: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind is not MeshArtifactKind.ENROLLMENT_APPROVAL:
            _fail("wrong_artifact_kind", "Mesh enrollment-approval kind is invalid")
        _common(
            protocol_version=self.protocol_version,
            pair_id=self.pair_id,
            space_id=self.space_id,
            membership_epoch=self.membership_epoch,
            issued_at_ms=self.issued_at_ms,
            nonce=self.nonce,
            source_public_key=self.source_public_key,
            source_fingerprint=self.source_fingerprint,
        )
        _identity(self.target_public_key, self.target_fingerprint, "target")
        if self.target_fingerprint == MESH_TARGET_UNBOUND:
            _fail("unbound_target_forbidden", "Mesh approval requires a target")
        if self.source_fingerprint == self.target_fingerprint:
            _fail("self_pairing_forbidden", "Mesh source and target must differ")
        _plain_string(
            self.invitation_digest, _DIGEST_RE, "invalid_invitation_digest"
        )
        _plain_string(self.join_claim_digest, _DIGEST_RE, "invalid_claim_digest")
        if self.invitation_digest == self.join_claim_digest:
            _fail("prior_digest_collision", "Mesh prior artifact digests must differ")
        object.__setattr__(
            self,
            "granted_scopes",
            _scopes(self.granted_scopes, "invalid_granted_scopes"),
        )

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "granted_scopes": list(self.granted_scopes),
            "invitation_digest": self.invitation_digest,
            "issued_at_ms": self.issued_at_ms,
            "join_claim_digest": self.join_claim_digest,
            "kind": self.kind.value,
            "membership_epoch": self.membership_epoch,
            "nonce": self.nonce,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
            "target_public_key": self.target_public_key,
        }


MeshArtifact: TypeAlias = MeshInvitation | MeshJoinClaim | MeshEnrollmentApproval


def artifact_canonical_bytes(artifact: MeshArtifact) -> bytes:
    if type(artifact) not in (MeshInvitation, MeshJoinClaim, MeshEnrollmentApproval):
        _fail("invalid_artifact", "Mesh artifact type is invalid")
    return canonical_dumps(artifact.as_dict())


def _domain(kind: MeshArtifactKind) -> bytes:
    return {
        MeshArtifactKind.INVITATION: INVITATION_SIGNATURE_DOMAIN,
        MeshArtifactKind.JOIN_CLAIM: JOIN_CLAIM_SIGNATURE_DOMAIN,
        MeshArtifactKind.ENROLLMENT_APPROVAL: ENROLLMENT_APPROVAL_SIGNATURE_DOMAIN,
    }[kind]


def _signer_public_key(artifact: MeshArtifact) -> str:
    # The target proves possession when claiming; source signs invitation and
    # approval.  This role split is part of the signed-triple authority model.
    if isinstance(artifact, MeshJoinClaim):
        return artifact.target_public_key
    return artifact.source_public_key


def _signature_encode(signature: bytes) -> str:
    if type(signature) is not bytes or len(signature) != 64:
        _fail("invalid_signature", "Mesh artifact signature is invalid")
    return base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")


def _signature_decode(value: object) -> bytes:
    if type(value) is not str or _B64URL_SIGNATURE_RE.fullmatch(value) is None:
        _fail("invalid_signature", "Mesh artifact signature is invalid")
    try:
        raw = base64.b64decode(value + "==", altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise MeshArtifactError(
            "invalid_signature", "Mesh artifact signature is invalid"
        ) from exc
    if len(raw) != 64 or _signature_encode(raw) != value:
        _fail("invalid_signature", "Mesh artifact signature is invalid")
    return raw


@dataclass(frozen=True, slots=True)
class SignedMeshArtifact:
    artifact: MeshArtifact
    signature: bytes

    def __post_init__(self) -> None:
        if type(self.artifact) not in (
            MeshInvitation,
            MeshJoinClaim,
            MeshEnrollmentApproval,
        ):
            _fail("invalid_artifact", "Mesh artifact type is invalid")
        if type(self.signature) is not bytes or len(self.signature) != 64:
            _fail("invalid_signature", "Mesh artifact signature is invalid")

    @classmethod
    def sign(
        cls, artifact: MeshArtifact, private_key: MeshPrivateKey
    ) -> "SignedMeshArtifact":
        if not isinstance(private_key, MeshPrivateKey):
            _fail("invalid_signer", "Mesh artifact signer is invalid")
        if private_key.public_key() != _signer_public_key(artifact):
            _fail("signer_identity_mismatch", "Mesh artifact signer does not match")
        payload = _domain(artifact.kind) + artifact_canonical_bytes(artifact)
        return cls(artifact=artifact, signature=private_key.sign(payload))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedMeshArtifact":
        try:
            value = canonical_loads(raw)
        except HCJError as exc:
            raise MeshArtifactError("invalid_artifact", "Mesh artifact is invalid") from exc
        if type(value) is not dict or frozenset(value) != {"artifact", "signature"}:
            _fail("invalid_artifact_shape", "Signed Mesh artifact shape is invalid")
        artifact_value = value["artifact"]
        if type(artifact_value) is not dict:
            _fail("invalid_artifact_shape", "Signed Mesh artifact shape is invalid")
        artifact = _artifact_from_mapping(artifact_value)
        return cls(artifact=artifact, signature=_signature_decode(value["signature"]))

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(
            {
                "artifact": self.artifact.as_dict(),
                "signature": _signature_encode(self.signature),
            }
        )

    def verify(self) -> None:
        payload = _domain(self.artifact.kind) + artifact_canonical_bytes(self.artifact)
        try:
            parse_mesh_public_key(_signer_public_key(self.artifact)).verify(
                self.signature, payload
            )
        except (InvalidSignature, MeshIdentityError, TypeError, ValueError) as exc:
            raise MeshArtifactError(
                "authentication_failed", "Mesh artifact authentication failed"
            ) from exc

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _artifact_from_mapping(value: dict[str, JSONValue]) -> MeshArtifact:
    kind_value = value.get("kind")
    try:
        kind = MeshArtifactKind(kind_value)
    except (ValueError, TypeError) as exc:
        raise MeshArtifactError("invalid_artifact_kind", "Mesh artifact kind is invalid") from exc

    fields = frozenset(value)
    if kind is MeshArtifactKind.INVITATION:
        if fields != _INVITATION_FIELDS:
            _fail("invalid_artifact_shape", "Mesh invitation shape is invalid")
        return MeshInvitation(kind=kind, **{k: v for k, v in value.items() if k != "kind"})  # type: ignore[arg-type]
    if kind is MeshArtifactKind.JOIN_CLAIM:
        if fields != _CLAIM_FIELDS:
            _fail("invalid_artifact_shape", "Mesh join-claim shape is invalid")
        values = {k: v for k, v in value.items() if k != "kind"}
        requested = values.pop("requested_scopes")
        return MeshJoinClaim(
            kind=kind,
            requested_scopes=tuple(requested) if type(requested) is list else requested,  # type: ignore[arg-type]
            **values,  # type: ignore[arg-type]
        )
    if fields != _APPROVAL_FIELDS:
        _fail("invalid_artifact_shape", "Mesh enrollment-approval shape is invalid")
    values = {k: v for k, v in value.items() if k != "kind"}
    granted = values.pop("granted_scopes")
    return MeshEnrollmentApproval(
        kind=kind,
        granted_scopes=tuple(granted) if type(granted) is list else granted,  # type: ignore[arg-type]
        **values,  # type: ignore[arg-type]
    )


def verify_artifact_chain(
    invitation: SignedMeshArtifact,
    claim: SignedMeshArtifact,
    approval: SignedMeshArtifact,
) -> None:
    """Verify the complete signed enrollment-intent triple without side effects.

    All signatures are proven before the cross-artifact bindings are examined.
    The helper deliberately performs no membership lookup; T17 eligibility is
    the separate :func:`verify_approval_authority` check against the applied
    source-space ``MembershipView``.
    """

    if (
        type(invitation) is not SignedMeshArtifact
        or type(invitation.artifact) is not MeshInvitation
        or type(claim) is not SignedMeshArtifact
        or type(claim.artifact) is not MeshJoinClaim
        or type(approval) is not SignedMeshArtifact
        or type(approval.artifact) is not MeshEnrollmentApproval
    ):
        _fail("invalid_artifact_chain", "Mesh artifact chain is invalid")

    invitation.verify()
    claim.verify()
    approval.verify()

    invitation_model = invitation.artifact
    claim_model = claim.artifact
    approval_model = approval.artifact
    invitation_digest = invitation.digest()
    claim_digest = claim.digest()

    common_invitation = (
        invitation_model.pair_id,
        invitation_model.space_id,
        invitation_model.membership_epoch,
        invitation_model.source_public_key,
        invitation_model.source_fingerprint,
    )
    common_claim = (
        claim_model.pair_id,
        claim_model.space_id,
        claim_model.membership_epoch,
        claim_model.source_public_key,
        claim_model.source_fingerprint,
    )
    common_approval = (
        approval_model.pair_id,
        approval_model.space_id,
        approval_model.membership_epoch,
        approval_model.source_public_key,
        approval_model.source_fingerprint,
    )
    if common_invitation != common_claim or common_claim != common_approval:
        _fail("artifact_chain_binding_mismatch", "Mesh artifact chain is invalid")
    if (
        claim_model.target_public_key != approval_model.target_public_key
        or claim_model.target_fingerprint != approval_model.target_fingerprint
    ):
        _fail("artifact_chain_binding_mismatch", "Mesh artifact chain is invalid")
    if (
        claim_model.invitation_digest != invitation_digest
        or approval_model.invitation_digest != invitation_digest
        or approval_model.join_claim_digest != claim_digest
    ):
        _fail("artifact_chain_digest_mismatch", "Mesh artifact chain is invalid")
    if not set(approval_model.granted_scopes).issubset(
        claim_model.requested_scopes
    ):
        _fail("artifact_chain_scope_escalation", "Mesh artifact chain is invalid")
    if not (
        invitation_model.issued_at_ms
        <= claim_model.issued_at_ms
        <= approval_model.issued_at_ms
        < invitation_model.expires_at_ms
    ):
        _fail("artifact_chain_time_mismatch", "Mesh artifact chain is invalid")


def verify_approval_authority(
    approval: MeshEnrollmentApproval,
    membership: MembershipView,
    *,
    enrollment_space_id: str,
) -> None:
    """Pure T17 authorization check; returns only on exact eligible authority.

    The initialized source space's exact MembershipView is the trust root.  The
    blank target, invitation, and claim never contribute an eligible key.  This
    function performs no state mutation and imports no lifecycle service.
    """

    if type(approval) is not MeshEnrollmentApproval or type(membership) is not MembershipView:
        _fail("source_not_authorized", "Mesh approval source is not authorized")
    if type(enrollment_space_id) is not str or enrollment_space_id != approval.space_id:
        _fail("source_not_authorized", "Mesh approval source is not authorized")
    if membership.protocol_version != 1 or membership.epoch != approval.membership_epoch:
        _fail("source_not_authorized", "Mesh approval source is not authorized")
    try:
        source_raw = decode_mesh_public_key(approval.source_public_key)
        target_raw = decode_mesh_public_key(approval.target_public_key)
    except (MeshIdentityError, TypeError, ValueError) as exc:
        raise MeshArtifactError(
            "source_not_authorized", "Mesh approval source is not authorized"
        ) from exc
    if source_raw == target_raw:
        _fail("source_not_authorized", "Mesh approval source is not authorized")

    eligible = []
    try:
        for member in membership.members:
            member_raw = decode_membership_public_key(member.public_key)
            if member_raw == source_raw:
                eligible.append(member)
    except (MeshIdentityError, TypeError, ValueError) as exc:
        # A malformed critical membership key makes the view ambiguous.
        raise MeshArtifactError(
            "source_not_authorized", "Mesh approval source is not authorized"
        ) from exc

    if len(eligible) != 1:
        _fail("source_not_authorized", "Mesh approval source is not authorized")
    member = eligible[0]
    if (
        member.status != MemberStatus.ACTIVE.value
        or not member.has_scope(PeerScope.COMMIT)
    ):
        _fail("source_not_authorized", "Mesh approval source is not authorized")


__all__ = [
    "ENROLLMENT_APPROVAL_SIGNATURE_DOMAIN",
    "INVITATION_SIGNATURE_DOMAIN",
    "JOIN_CLAIM_SIGNATURE_DOMAIN",
    "MESH_INVITATION_TTL_MILLISECONDS",
    "MESH_TARGET_UNBOUND",
    "MeshArtifact",
    "MeshArtifactError",
    "MeshArtifactKind",
    "MeshEnrollmentApproval",
    "MeshInvitation",
    "MeshJoinClaim",
    "SignedMeshArtifact",
    "artifact_canonical_bytes",
    "verify_artifact_chain",
    "verify_approval_authority",
]
