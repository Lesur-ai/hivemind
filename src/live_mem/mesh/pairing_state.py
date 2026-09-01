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
import hashlib
import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from .canonical import HCJError, canonical_dumps, canonical_loads
from .identity import (
    MeshIdentityError,
    MeshPrivateKey,
    decode_membership_public_key,
    mesh_identity_fingerprint,
    parse_mesh_public_key,
)

_PAIR_ID_RE = re.compile(r"^pair_[0-9a-f]{32}$", re.ASCII)
_PREPARATION_ID_RE = re.compile(r"^prep_[0-9a-f]{32}$", re.ASCII)
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)
_FINGERPRINT_RE = re.compile(r"^hm1:[0-9a-f]{64}$", re.ASCII)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PUBLIC_KEY_RE = re.compile(r"^ed25519-public:v1:[A-Za-z0-9_-]{43}$", re.ASCII)
_MEMBERSHIP_PUBLIC_KEY_RE = re.compile(r"^ed25519:[A-Za-z0-9_-]{43}$", re.ASCII)
_NODE_ID_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_EVENT_ID_RE = re.compile(r"^[0-9a-f-]{1,64}$", re.ASCII)
_SCOPES = ("read", "propose", "commit")
_NEXT_ACTIONS = ("resume", "resync", "evict")

#: Domain separator for the signed blocked-recovery evidence structure.
_BLOCKED_RECOVERY_DOMAIN = b"hivemind-mesh-blocked-recovery-v1\0"
#: Domain separator for the source's immutable export/final-ACK binding.
_SOURCE_BOOTSTRAP_EVIDENCE_DOMAIN = b"hivemind-mesh-source-bootstrap-evidence-v1\0"
#: Domain separator for the durable per-space source activation index.
_SOURCE_ACTIVATION_MIGRATION_DOMAIN = b"hivemind-mesh-source-activation-migration-v1\0"
#: Domain separator for a target's completed e+2 activation receipt.
_TARGET_ACTIVATION_RECEIPT_DOMAIN = b"hivemind-mesh-target-activation-receipt-v1\0"
#: Domain separator for the source's durable all-ACK terminal confirmation.
_SOURCE_ACTIVATION_RECEIPT_DOMAIN = b"hivemind-mesh-source-activation-receipt-v1\0"
#: Domain separator for the target's readback of the source terminal receipt.
_TARGET_TERMINAL_CONFIRMATION_DOMAIN = b"hivemind-mesh-target-terminal-confirmation-v1\0"
#: Domain separator for a source's signed, target-readable terminal disposition.
_SOURCE_TERMINAL_DISPOSITION_DOMAIN = b"hivemind-mesh-source-terminal-disposition-v1\0"
#: Domain separator for a source's pre-mutation PENDING-eviction intent.
_SOURCE_PENDING_EVICTION_INTENT_DOMAIN = b"hivemind-mesh-source-pending-eviction-intent-v1\0"
#: Domain separator for a source cancellation made before it knows the target.
_SOURCE_PRECLAIM_CANCEL_BARRIER_DOMAIN = b"hivemind-mesh-source-preclaim-cancel-barrier-v1\0"
#: Domain separator for the bounded per-space target ordinary-write fence.
_TARGET_PAIRING_FENCE_DOMAIN = b"hivemind-mesh-target-pairing-fence-v1\0"
#: Domain separator for the permanent per-space #417 target discriminator.
_TARGET_PAIRING_ADMISSION_ANCHOR_DOMAIN = (
    b"hivemind-mesh-target-pairing-admission-anchor-v1\0"
)
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


class SourcePreparationState(str, Enum):
    """Closed lifecycle for an existing-space source preparation intent."""

    PREPARING = "preparing"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class SourcePreparationIntent:
    """Durable public evidence for one additive source-genesis transition.

    The record freezes every value used to build the expected Hivemind genesis.
    A restart can therefore resume only the exact transition it began, without
    inventing a new timestamp or silently rotating identity.
    ``completed_at_ms == 0`` is the sole not-completed sentinel.
    """

    preparation_id: str
    protocol_version: int
    state: str
    space_id: str
    source_fingerprint: str
    membership_public_key: str
    node_id: str
    display_name: str
    public_url: str
    started_at_ms: int
    started_at_iso: str
    completed_at_ms: int
    expected_state_token: str

    def __post_init__(self) -> None:
        _require(
            type(self.preparation_id) is str
            and _PREPARATION_ID_RE.fullmatch(self.preparation_id) is not None,
            "invalid_preparation_id",
            "invalid source preparation id",
        )
        _require(
            type(self.protocol_version) is int
            and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_version",
            "unsupported source preparation version",
        )
        _require(
            type(self.state) is str
            and self.state in (state.value for state in SourcePreparationState),
            "invalid_state",
            "invalid source preparation state",
        )
        _require(
            type(self.space_id) is str
            and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_space_id",
            "invalid source preparation space id",
        )
        _require(
            type(self.source_fingerprint) is str
            and _FINGERPRINT_RE.fullmatch(self.source_fingerprint) is not None,
            "invalid_source_fp",
            "invalid source preparation fingerprint",
        )
        _require(
            type(self.membership_public_key) is str
            and _MEMBERSHIP_PUBLIC_KEY_RE.fullmatch(self.membership_public_key)
            is not None,
            "invalid_membership_key",
            "invalid source preparation membership key",
        )
        try:
            decode_membership_public_key(self.membership_public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_membership_key",
                "invalid source preparation membership key",
            ) from exc
        strict_public_key = "ed25519-public:v1:" + self.membership_public_key.split(
            ":", 1
        )[1]
        _require(
            mesh_identity_fingerprint(strict_public_key) == self.source_fingerprint,
            "identity_mismatch",
            "source preparation key does not match its fingerprint",
        )
        expected_node_id = self.source_fingerprint.split(":", 1)[1]
        _require(
            type(self.node_id) is str
            and _NODE_ID_RE.fullmatch(self.node_id) is not None
            and self.node_id == expected_node_id,
            "invalid_node_id",
            "source preparation node id does not match its fingerprint",
        )
        try:
            encoded_display_name = self.display_name.encode("utf-8", "strict")
        except (AttributeError, UnicodeEncodeError) as exc:
            raise MeshPairingError(
                "invalid_display_name",
                "invalid source preparation display name",
            ) from exc
        _require(
            type(self.display_name) is str
            and bool(self.display_name)
            and len(encoded_display_name) <= 128
            and unicodedata.normalize("NFC", self.display_name) == self.display_name
            and not any(
                unicodedata.category(char).startswith("C")
                or char in {"\u2028", "\u2029"}
                for char in self.display_name
            ),
            "invalid_display_name",
            "invalid source preparation display name",
        )
        _require(
            type(self.public_url) is str
            and self.public_url.startswith("https://")
            and len(self.public_url) <= 2048,
            "invalid_public_url",
            "invalid source preparation public URL",
        )
        _require(
            type(self.started_at_ms) is int and self.started_at_ms >= 0,
            "invalid_timestamp",
            "invalid source preparation start timestamp",
        )
        _require(
            type(self.started_at_iso) is str and bool(self.started_at_iso),
            "invalid_timestamp",
            "invalid source preparation ISO timestamp",
        )
        try:
            parsed_start = datetime.fromisoformat(
                self.started_at_iso.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise MeshPairingError(
                "invalid_timestamp", "invalid source preparation ISO timestamp"
            ) from exc
        _require(
            parsed_start.tzinfo is not None
            and parsed_start.utcoffset() == timezone.utc.utcoffset(parsed_start)
            and parsed_start.microsecond % 1000 == 0
            and int(parsed_start.timestamp() * 1000) == self.started_at_ms,
            "invalid_timestamp",
            "source preparation timestamps do not identify the same instant",
        )
        _require(
            type(self.completed_at_ms) is int and self.completed_at_ms >= 0,
            "invalid_timestamp",
            "invalid source preparation completion timestamp",
        )
        if self.state == SourcePreparationState.PREPARING.value:
            _require(
                self.completed_at_ms == 0,
                "invalid_timestamp",
                "preparing source preparation cannot have a completion timestamp",
            )
        else:
            _require(
                self.completed_at_ms >= self.started_at_ms
                and self.completed_at_ms > 0,
                "invalid_timestamp",
                "completed source preparation has an invalid completion timestamp",
            )
        _require(
            type(self.expected_state_token) is str
            and _DIGEST_RE.fullmatch(self.expected_state_token) is not None,
            "invalid_state_token",
            "invalid source preparation state token",
        )

    @property
    def state_enum(self) -> SourcePreparationState:
        return SourcePreparationState(self.state)

    def complete(self, now_ms: int) -> "SourcePreparationIntent":
        """Advance PREPARING -> COMPLETE without changing frozen evidence."""

        _require(
            self.state_enum is SourcePreparationState.PREPARING,
            "illegal_transition",
            "source preparation is not preparing",
        )
        _require(
            type(now_ms) is int and now_ms >= self.started_at_ms and now_ms > 0,
            "invalid_timestamp",
            "invalid source preparation completion timestamp",
        )
        return replace(
            self,
            state=SourcePreparationState.COMPLETE.value,
            completed_at_ms=now_ms,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "completed_at_ms": self.completed_at_ms,
            "display_name": self.display_name,
            "expected_state_token": self.expected_state_token,
            "membership_public_key": self.membership_public_key,
            "node_id": self.node_id,
            "preparation_id": self.preparation_id,
            "protocol_version": self.protocol_version,
            "public_url": self.public_url,
            "source_fingerprint": self.source_fingerprint,
            "space_id": self.space_id,
            "started_at_iso": self.started_at_iso,
            "started_at_ms": self.started_at_ms,
            "state": self.state,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourcePreparationIntent":
        _require(
            isinstance(data, Mapping),
            "invalid_preparation",
            "source preparation must be a mapping",
        )
        expected_fields = {
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
        _require(
            set(data) == expected_fields,
            "invalid_preparation",
            "source preparation fields are incomplete or unknown",
        )
        return cls(
            preparation_id=data["preparation_id"],
            protocol_version=data["protocol_version"],
            state=data["state"],
            space_id=data["space_id"],
            source_fingerprint=data["source_fingerprint"],
            membership_public_key=data["membership_public_key"],
            node_id=data["node_id"],
            display_name=data["display_name"],
            public_url=data["public_url"],
            started_at_ms=data["started_at_ms"],
            started_at_iso=data["started_at_iso"],
            completed_at_ms=data["completed_at_ms"],
            expected_state_token=data["expected_state_token"],
        )


def _check_commit_binding(
    *,
    bank_version: Any,
    commit_id: Any,
    commit_digest: Any,
    code: str,
) -> tuple[int, str, str]:
    """Validate a bank pointer / selected-commit binding.

    The pairing records below intentionally preserve the *exact* selected
    commit, including the no-commit ``(-1, "", "")`` sentinel.  A snapshot
    cannot be activated from a pointer that merely has the same bank version.
    """

    _require(
        type(bank_version) is int and bank_version >= -1,
        code,
        "invalid bank version",
    )
    commit_id = _check_str(commit_id, allow_empty=True)
    _require(
        len(commit_id) <= 256 and all(ord(char) >= 0x20 for char in commit_id),
        code,
        "invalid commit id",
    )
    commit_digest = _check_digest(commit_digest, allow_empty=True)
    if bank_version == -1:
        _require(
            commit_id == "" and commit_digest == "",
            code,
            "empty bank pointer has a commit binding",
        )
    else:
        _require(
            commit_id != "" and commit_digest != "",
            code,
            "selected bank commit is incomplete",
        )
    return bank_version, commit_id, commit_digest


@dataclass(frozen=True, slots=True)
class SourceActivationMigrationAuthority:
    """Source-signed ownership of one current #417 activation tail.

    The legacy activation-migration index originally stored only an untrusted
    pair id.  #417 re-arms that bounded per-space index before Transition 1 so
    it can fence a delayed e+2 even when mutable fence/marker records vanish.
    This signed authority prevents a valid-schema rewrite from silently
    downgrading that active owner to legacy compatibility.
    """

    pair_id: str
    protocol_version: int
    space_id: str
    source_fingerprint: str
    target_fingerprint: str
    base_epoch: int
    requires_terminal_confirmation: bool
    issued_at_ms: int

    def __post_init__(self) -> None:
        _require(
            type(self.pair_id) is str
            and _PAIR_ID_RE.fullmatch(self.pair_id) is not None,
            "invalid_source_activation_migration",
            "invalid source activation migration pair id",
        )
        _require(
            type(self.protocol_version) is int
            and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_source_activation_migration",
            "unsupported source activation migration version",
        )
        _require(
            type(self.space_id) is str
            and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_source_activation_migration",
            "invalid source activation migration space id",
        )
        for value, label in (
            (self.source_fingerprint, "source"),
            (self.target_fingerprint, "target"),
        ):
            _require(
                type(value) is str and _FINGERPRINT_RE.fullmatch(value) is not None,
                "invalid_source_activation_migration",
                f"invalid {label} activation migration fingerprint",
            )
        _require(
            type(self.base_epoch) is int and self.base_epoch >= 0,
            "invalid_source_activation_migration",
            "invalid source activation migration epoch",
        )
        _require(
            type(self.requires_terminal_confirmation) is bool
            and self.requires_terminal_confirmation,
            "invalid_source_activation_migration",
            "source activation migration must require terminal confirmation",
        )
        _require(
            type(self.issued_at_ms) is int and self.issued_at_ms >= 0,
            "invalid_source_activation_migration",
            "invalid source activation migration timestamp",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_epoch": self.base_epoch,
            "issued_at_ms": self.issued_at_ms,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "requires_terminal_confirmation": self.requires_terminal_confirmation,
            "source_fingerprint": self.source_fingerprint,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "SourceActivationMigrationAuthority":
        expected_fields = {
            "base_epoch",
            "issued_at_ms",
            "pair_id",
            "protocol_version",
            "requires_terminal_confirmation",
            "source_fingerprint",
            "space_id",
            "target_fingerprint",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected_fields,
            "invalid_source_activation_migration",
            "source activation migration fields are incomplete or unknown",
        )
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class SourceBootstrapEvidence:
    """Immutable, read-back-verified source snapshot binding.

    This record closes the intentional network gap between exporting an e+1
    bootstrap and accepting the target's final ACK.  It is local protocol
    authority, separate from the backward-compatible operational session, and
    carries only public identifiers plus canonical digests.
    """

    pair_id: str
    protocol_version: int
    space_id: str
    source_fingerprint: str
    target_fingerprint: str
    membership_epoch: int
    membership_snapshot_digest: str
    membership_view_digest: str
    manifest_digest: str
    bank_version: int
    commit_id: str
    node_digest: str
    term: int
    term_digest: str
    token_state: str
    token_term: int
    token_fencing_token: int
    token_membership_epoch: int
    token_bank_version: int
    token_digest: str
    pointer_bank_version: int
    pointer_commit_id: str
    pointer_digest: str
    selected_commit_digest: str
    preparation_digest: str
    health_digest: str
    recorded_at_ms: int

    def __post_init__(self) -> None:
        _require(
            type(self.pair_id) is str
            and _PAIR_ID_RE.fullmatch(self.pair_id) is not None,
            "invalid_source_evidence",
            "invalid source bootstrap pair id",
        )
        _require(
            type(self.protocol_version) is int
            and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_source_evidence",
            "unsupported source bootstrap evidence version",
        )
        _require(
            type(self.space_id) is str
            and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_source_evidence",
            "invalid source bootstrap space id",
        )
        for value, label in (
            (self.source_fingerprint, "source"),
            (self.target_fingerprint, "target"),
        ):
            _require(
                type(value) is str and _FINGERPRINT_RE.fullmatch(value) is not None,
                "invalid_source_evidence",
                f"invalid {label} bootstrap fingerprint",
            )
        _require(
            type(self.membership_epoch) is int and self.membership_epoch >= 0,
            "invalid_source_evidence",
            "invalid source bootstrap membership epoch",
        )
        _check_digest(self.membership_snapshot_digest, allow_empty=False)
        _check_digest(self.membership_view_digest, allow_empty=False)
        _check_digest(self.manifest_digest, allow_empty=False)
        _check_digest(self.node_digest, allow_empty=False)
        _check_digest(self.term_digest, allow_empty=False)
        _check_digest(self.pointer_digest, allow_empty=False)
        _check_digest(self.preparation_digest, allow_empty=True)
        _check_digest(self.health_digest, allow_empty=True)
        bank_version, commit_id, commit_digest = _check_commit_binding(
            bank_version=self.bank_version,
            commit_id=self.commit_id,
            commit_digest=self.selected_commit_digest,
            code="invalid_source_evidence",
        )
        pointer_bank_version, pointer_commit_id, _ = _check_commit_binding(
            bank_version=self.pointer_bank_version,
            commit_id=self.pointer_commit_id,
            commit_digest=self.selected_commit_digest,
            code="invalid_source_evidence",
        )
        _require(
            (bank_version, commit_id)
            == (pointer_bank_version, pointer_commit_id),
            "invalid_source_evidence",
            "source bootstrap pointer does not match its manifest",
        )
        for value, label in (
            (self.term, "term"),
            (self.token_term, "token term"),
            (self.token_fencing_token, "token fencing token"),
            (self.token_membership_epoch, "token membership epoch"),
        ):
            _require(
                type(value) is int and value >= 0,
                "invalid_source_evidence",
                f"invalid source bootstrap {label}",
            )
        _require(
            type(self.token_bank_version) is int
            and self.token_bank_version >= -1,
            "invalid_source_evidence",
            "invalid source bootstrap token bank version",
        )
        _require(
            type(self.token_state) is str
            and self.token_state in {"absent", "free", "held", "releasing"},
            "invalid_source_evidence",
            "invalid source bootstrap token state",
        )
        _check_digest(self.token_digest, allow_empty=False)
        _require(
            type(self.recorded_at_ms) is int and self.recorded_at_ms >= 0,
            "invalid_source_evidence",
            "invalid source bootstrap timestamp",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bank_version": self.bank_version,
            "commit_id": self.commit_id,
            "manifest_digest": self.manifest_digest,
            "membership_epoch": self.membership_epoch,
            "membership_snapshot_digest": self.membership_snapshot_digest,
            "membership_view_digest": self.membership_view_digest,
            "node_digest": self.node_digest,
            "pair_id": self.pair_id,
            "pointer_bank_version": self.pointer_bank_version,
            "pointer_commit_id": self.pointer_commit_id,
            "pointer_digest": self.pointer_digest,
            "preparation_digest": self.preparation_digest,
            "protocol_version": self.protocol_version,
            "recorded_at_ms": self.recorded_at_ms,
            "selected_commit_digest": self.selected_commit_digest,
            "health_digest": self.health_digest,
            "source_fingerprint": self.source_fingerprint,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
            "term": self.term,
            "term_digest": self.term_digest,
            "token_bank_version": self.token_bank_version,
            "token_digest": self.token_digest,
            "token_fencing_token": self.token_fencing_token,
            "token_membership_epoch": self.token_membership_epoch,
            "token_state": self.token_state,
            "token_term": self.token_term,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceBootstrapEvidence":
        expected_fields = {
            "bank_version",
            "commit_id",
            "manifest_digest",
            "membership_epoch",
            "membership_snapshot_digest",
            "membership_view_digest",
            "node_digest",
            "pair_id",
            "pointer_bank_version",
            "pointer_commit_id",
            "pointer_digest",
            "preparation_digest",
            "protocol_version",
            "recorded_at_ms",
            "selected_commit_digest",
            "health_digest",
            "source_fingerprint",
            "space_id",
            "target_fingerprint",
            "term",
            "term_digest",
            "token_bank_version",
            "token_digest",
            "token_fencing_token",
            "token_membership_epoch",
            "token_state",
            "token_term",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected_fields,
            "invalid_source_evidence",
            "source bootstrap evidence fields are incomplete or unknown",
        )
        return cls(**dict(data))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SourceBootstrapEvidence":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class ImportValidatedAuthority:
    """Durable proof that this exact target imported and validated e+1.

    A target session is operational state only.  This authority is written only
    after the importer has validated the signed snapshot and its authoritative
    local readback, and is required for every self-activation/finalization tail.
    """

    pair_id: str
    protocol_version: int
    space_id: str
    source_fingerprint: str
    target_fingerprint: str
    local_node_id: str
    membership_epoch: int
    membership_snapshot_digest: str
    membership_view_digest: str
    manifest_digest: str
    bank_version: int
    commit_id: str
    term_digest: str
    token_digest: str
    pointer_digest: str
    selected_commit_digest: str
    validated_at_ms: int

    def __post_init__(self) -> None:
        _require(
            type(self.pair_id) is str
            and _PAIR_ID_RE.fullmatch(self.pair_id) is not None,
            "invalid_import_validation",
            "invalid import validation pair id",
        )
        _require(
            type(self.protocol_version) is int
            and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_import_validation",
            "unsupported import validation version",
        )
        _require(
            type(self.space_id) is str
            and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_import_validation",
            "invalid import validation space id",
        )
        for value, label in (
            (self.source_fingerprint, "source"),
            (self.target_fingerprint, "target"),
        ):
            _require(
                type(value) is str and _FINGERPRINT_RE.fullmatch(value) is not None,
                "invalid_import_validation",
                f"invalid import validation {label} fingerprint",
            )
        _require(
            type(self.local_node_id) is str
            and _NODE_ID_RE.fullmatch(self.local_node_id) is not None
            and self.local_node_id == self.target_fingerprint.split(":", 1)[1],
            "invalid_import_validation",
            "import validation local node does not match target identity",
        )
        _require(
            type(self.membership_epoch) is int and self.membership_epoch >= 0,
            "invalid_import_validation",
            "invalid import validation membership epoch",
        )
        _check_digest(self.membership_snapshot_digest, allow_empty=False)
        _check_digest(self.membership_view_digest, allow_empty=False)
        _check_digest(self.manifest_digest, allow_empty=False)
        _check_digest(self.term_digest, allow_empty=False)
        _check_digest(self.token_digest, allow_empty=False)
        _check_digest(self.pointer_digest, allow_empty=False)
        _check_commit_binding(
            bank_version=self.bank_version,
            commit_id=self.commit_id,
            commit_digest=self.selected_commit_digest,
            code="invalid_import_validation",
        )
        _require(
            type(self.validated_at_ms) is int and self.validated_at_ms >= 0,
            "invalid_import_validation",
            "invalid import validation timestamp",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "bank_version": self.bank_version,
            "commit_id": self.commit_id,
            "local_node_id": self.local_node_id,
            "manifest_digest": self.manifest_digest,
            "membership_epoch": self.membership_epoch,
            "membership_snapshot_digest": self.membership_snapshot_digest,
            "membership_view_digest": self.membership_view_digest,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "pointer_digest": self.pointer_digest,
            "source_fingerprint": self.source_fingerprint,
            "selected_commit_digest": self.selected_commit_digest,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
            "term_digest": self.term_digest,
            "token_digest": self.token_digest,
            "validated_at_ms": self.validated_at_ms,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImportValidatedAuthority":
        expected_fields = {
            "bank_version",
            "commit_id",
            "local_node_id",
            "manifest_digest",
            "membership_epoch",
            "membership_snapshot_digest",
            "membership_view_digest",
            "pair_id",
            "protocol_version",
            "pointer_digest",
            "selected_commit_digest",
            "source_fingerprint",
            "space_id",
            "target_fingerprint",
            "term_digest",
            "token_digest",
            "validated_at_ms",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected_fields,
            "invalid_import_validation",
            "import validation fields are incomplete or unknown",
        )
        return cls(**dict(data))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ImportValidatedAuthority":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class TargetActivationReceipt:
    """Target-signed terminal proof for a completed e+2 activation tail.

    ``ImportValidatedAuthority`` remains the mandatory gate for the first
    promotion.  This receipt is written only after that gate has been matched
    against the local e+2 membership and before the terminal session/release
    tail.  It lets a later marker-loss crash tail recover without treating a
    mutable ``ACTIVE`` session as authority.
    """

    authority: ImportValidatedAuthority
    membership_epoch: int
    membership_view_digest: str
    activated_at_ms: int

    def __post_init__(self) -> None:
        _require(
            isinstance(self.authority, ImportValidatedAuthority),
            "invalid_activation_receipt",
            "target activation receipt authority is invalid",
        )
        _require(
            type(self.membership_epoch) is int
            and self.membership_epoch == self.authority.membership_epoch + 1,
            "invalid_activation_receipt",
            "target activation receipt epoch is invalid",
        )
        _check_digest(self.membership_view_digest, allow_empty=False)
        _require(
            type(self.activated_at_ms) is int
            and self.activated_at_ms >= self.authority.validated_at_ms,
            "invalid_activation_receipt",
            "target activation receipt timestamp is invalid",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "activated_at_ms": self.activated_at_ms,
            "authority": self.authority.as_dict(),
            "membership_epoch": self.membership_epoch,
            "membership_view_digest": self.membership_view_digest,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TargetActivationReceipt":
        expected_fields = {
            "activated_at_ms",
            "authority",
            "membership_epoch",
            "membership_view_digest",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected_fields,
            "invalid_activation_receipt",
            "target activation receipt fields are incomplete or unknown",
        )
        return cls(
            authority=ImportValidatedAuthority.from_dict(data["authority"]),
            membership_epoch=data["membership_epoch"],
            membership_view_digest=data["membership_view_digest"],
            activated_at_ms=data["activated_at_ms"],
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "TargetActivationReceipt":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SourceActivationReceipt:
    """Source-signed proof that the all-ACK activation tail reached source e+2.

    The target keeps its ordinary-write reservation after it applies e+2.  It
    releases that fence only after receiving this record, which the source may
    create only after it has durably recorded its own terminal ``ACTIVE``
    session and verified the target's signed e+2 receipt.  Binding the latter
    receipt's digest prevents a source-signed retry from authorizing a different
    target terminal state.
    """

    pair_id: str
    protocol_version: int
    space_id: str
    source_fingerprint: str
    target_fingerprint: str
    source_node_id: str
    target_node_id: str
    base_epoch: int
    membership_epoch: int
    activation_event_id: str
    membership_view_digest: str
    target_activation_receipt_digest: str
    target_activation_receipt: Mapping[str, Any]
    confirmed_at_ms: int

    def __post_init__(self) -> None:
        _require(
            type(self.pair_id) is str and _PAIR_ID_RE.fullmatch(self.pair_id) is not None,
            "invalid_source_activation_receipt",
            "invalid source activation receipt pair id",
        )
        _require(
            type(self.protocol_version) is int and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_source_activation_receipt",
            "unsupported source activation receipt version",
        )
        _require(
            type(self.space_id) is str and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_source_activation_receipt",
            "invalid source activation receipt space id",
        )
        for value, label in (
            (self.source_fingerprint, "source"),
            (self.target_fingerprint, "target"),
        ):
            _require(
                type(value) is str and _FINGERPRINT_RE.fullmatch(value) is not None,
                "invalid_source_activation_receipt",
                f"invalid {label} activation receipt fingerprint",
            )
        _require(
            type(self.source_node_id) is str
            and 0 < len(self.source_node_id) <= 256
            and "/" not in self.source_node_id,
            "invalid_source_activation_receipt",
            "source activation receipt source identity is invalid",
        )
        _require(
            type(self.target_node_id) is str
            and 0 < len(self.target_node_id) <= 256
            and "/" not in self.target_node_id
            and self.target_node_id == self.target_fingerprint.split(":", 1)[1],
            "invalid_source_activation_receipt",
            "source activation receipt target identity is invalid",
        )
        _require(
            type(self.base_epoch) is int and self.base_epoch >= 0,
            "invalid_source_activation_receipt",
            "source activation receipt base epoch is invalid",
        )
        _require(
            type(self.membership_epoch) is int
            and self.membership_epoch == self.base_epoch + 2,
            "invalid_source_activation_receipt",
            "source activation receipt epoch is invalid",
        )
        _require(
            type(self.activation_event_id) is str
            and _EVENT_ID_RE.fullmatch(self.activation_event_id) is not None,
            "invalid_source_activation_receipt",
            "source activation receipt event id is invalid",
        )
        _check_digest(self.membership_view_digest, allow_empty=False)
        _check_digest(self.target_activation_receipt_digest, allow_empty=False)
        # Keep the exact target-signed receipt, not only its digest.  If the
        # target loses its local copy after the source has durably reached e+2
        # but before it receives this record, the source can redeliver the
        # immutable signed bytes and the target can restore the exact authority
        # rather than minting a timestamp-different replacement.
        try:
            target_receipt = SignedTargetActivationReceipt.from_dict(
                self.target_activation_receipt
            )
        except (MeshPairingError, TypeError, ValueError) as exc:
            raise MeshPairingError(
                "invalid_source_activation_receipt",
                "source activation receipt target proof is invalid",
            ) from exc
        if hashlib.sha256(target_receipt.canonical_bytes()).hexdigest() != (
            self.target_activation_receipt_digest
        ):
            raise MeshPairingError(
                "invalid_source_activation_receipt",
                "source activation receipt target proof digest is invalid",
            )
        object.__setattr__(self, "target_activation_receipt", target_receipt.as_dict())
        _require(
            type(self.confirmed_at_ms) is int and self.confirmed_at_ms >= 0,
            "invalid_source_activation_receipt",
            "source activation receipt timestamp is invalid",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "activation_event_id": self.activation_event_id,
            "base_epoch": self.base_epoch,
            "confirmed_at_ms": self.confirmed_at_ms,
            "membership_epoch": self.membership_epoch,
            "membership_view_digest": self.membership_view_digest,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "source_fingerprint": self.source_fingerprint,
            "source_node_id": self.source_node_id,
            "space_id": self.space_id,
            "target_activation_receipt": dict(self.target_activation_receipt),
            "target_activation_receipt_digest": self.target_activation_receipt_digest,
            "target_fingerprint": self.target_fingerprint,
            "target_node_id": self.target_node_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceActivationReceipt":
        expected_fields = {
            "activation_event_id",
            "base_epoch",
            "confirmed_at_ms",
            "membership_epoch",
            "membership_view_digest",
            "pair_id",
            "protocol_version",
            "source_fingerprint",
            "source_node_id",
            "space_id",
            "target_activation_receipt",
            "target_activation_receipt_digest",
            "target_fingerprint",
            "target_node_id",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected_fields,
            "invalid_source_activation_receipt",
            "source activation receipt fields are incomplete or unknown",
        )
        return cls(**dict(data))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SourceActivationReceipt":
        return cls.from_dict(canonical_loads(raw))


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
        _require(
            type(self.space_id) is str
            and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_space_id",
            "pairing space_id is invalid",
        )
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
class SignedSourceActivationMigrationAuthority:
    """Detached source signature over a current source-tail index record."""

    authority: SourceActivationMigrationAuthority
    signature: str

    @classmethod
    def sign(
        cls,
        authority: SourceActivationMigrationAuthority,
        private_key: MeshPrivateKey,
    ) -> "SignedSourceActivationMigrationAuthority":
        signature = private_key.sign(
            _SOURCE_ACTIVATION_MIGRATION_DOMAIN + authority.canonical_bytes()
        )
        return cls(authority=authority, signature=_b64url_no_pad(signature))

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid source activation migration signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw,
                _SOURCE_ACTIVATION_MIGRATION_DOMAIN
                + self.authority.canonical_bytes(),
            )
        except Exception as exc:  # cryptography raises InvalidSignature
            raise MeshPairingError(
                "bad_signature", "source activation migration signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"authority": self.authority.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "SignedSourceActivationMigrationAuthority":
        _require(
            isinstance(data, Mapping) and set(data) == {"authority", "signature"},
            "invalid_source_activation_migration",
            "signed source activation migration is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature",
                "source activation migration signature is invalid",
            )
        return cls(
            authority=SourceActivationMigrationAuthority.from_dict(data["authority"]),
            signature=signature,
        )


@dataclass(frozen=True, slots=True)
class SignedSourceBootstrapEvidence:
    """A source export binding authenticated by the local Mesh identity.

    The signed bootstrap envelope anchors shared snapshot bytes. This companion
    signature anchors the source-local node/token observations that deliberately
    do not travel in that snapshot, so a valid-schema storage rewrite cannot
    replace both the live state and the local evidence after a crash.
    """

    evidence: SourceBootstrapEvidence
    signature: str

    @classmethod
    def sign(
        cls, evidence: SourceBootstrapEvidence, private_key: MeshPrivateKey
    ) -> "SignedSourceBootstrapEvidence":
        signature = private_key.sign(
            _SOURCE_BOOTSTRAP_EVIDENCE_DOMAIN + evidence.canonical_bytes()
        )
        return cls(evidence=evidence, signature=_b64url_no_pad(signature))

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid source-evidence signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw,
                _SOURCE_BOOTSTRAP_EVIDENCE_DOMAIN
                + self.evidence.canonical_bytes(),
            )
        except Exception as exc:  # cryptography raises InvalidSignature
            raise MeshPairingError(
                "bad_signature", "source bootstrap evidence signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"evidence": self.evidence.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SignedSourceBootstrapEvidence":
        _require(
            isinstance(data, Mapping) and set(data) == {"evidence", "signature"},
            "invalid_source_evidence",
            "signed source bootstrap evidence is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "source bootstrap evidence signature is invalid"
            )
        return cls(
            evidence=SourceBootstrapEvidence.from_dict(data["evidence"]),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedSourceBootstrapEvidence":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SignedTargetActivationReceipt:
    """A target's detached signature over its terminal activation receipt."""

    receipt: TargetActivationReceipt
    signature: str

    @classmethod
    def sign(
        cls, receipt: TargetActivationReceipt, private_key: MeshPrivateKey
    ) -> "SignedTargetActivationReceipt":
        signature = private_key.sign(
            _TARGET_ACTIVATION_RECEIPT_DOMAIN + receipt.canonical_bytes()
        )
        return cls(receipt=receipt, signature=_b64url_no_pad(signature))

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid activation-receipt signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw,
                _TARGET_ACTIVATION_RECEIPT_DOMAIN
                + self.receipt.canonical_bytes(),
            )
        except Exception as exc:  # cryptography raises InvalidSignature
            raise MeshPairingError(
                "bad_signature", "target activation receipt signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"receipt": self.receipt.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SignedTargetActivationReceipt":
        _require(
            isinstance(data, Mapping) and set(data) == {"receipt", "signature"},
            "invalid_activation_receipt",
            "signed target activation receipt is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "target activation receipt signature is invalid"
            )
        return cls(
            receipt=TargetActivationReceipt.from_dict(data["receipt"]),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedTargetActivationReceipt":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SignedSourceActivationReceipt:
    """A source's detached signature over its terminal all-ACK confirmation."""

    receipt: SourceActivationReceipt
    signature: str

    @classmethod
    def sign(
        cls, receipt: SourceActivationReceipt, private_key: MeshPrivateKey
    ) -> "SignedSourceActivationReceipt":
        signature = private_key.sign(
            _SOURCE_ACTIVATION_RECEIPT_DOMAIN + receipt.canonical_bytes()
        )
        return cls(receipt=receipt, signature=_b64url_no_pad(signature))

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid source activation-receipt signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw,
                _SOURCE_ACTIVATION_RECEIPT_DOMAIN
                + self.receipt.canonical_bytes(),
            )
        except Exception as exc:  # cryptography raises InvalidSignature
            raise MeshPairingError(
                "bad_signature", "source activation receipt signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"receipt": self.receipt.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SignedSourceActivationReceipt":
        _require(
            isinstance(data, Mapping) and set(data) == {"receipt", "signature"},
            "invalid_source_activation_receipt",
            "signed source activation receipt is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "source activation receipt signature is invalid"
            )
        return cls(
            receipt=SourceActivationReceipt.from_dict(data["receipt"]),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedSourceActivationReceipt":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class TargetTerminalConfirmationReceipt:
    """Target-signed readback proof for the final all-ACK release.

    A source does not clear its own activation fence merely because its local
    session is ``ACTIVE`` or because a mutable fence record disappeared.  This
    receipt proves that the addressed target read back the exact source-signed
    terminal record and its bound target receipt before releasing the target
    ordinary-write reservation.
    """

    pair_id: str
    protocol_version: int
    space_id: str
    source_fingerprint: str
    target_fingerprint: str
    base_epoch: int
    membership_epoch: int
    source_activation_receipt_digest: str
    target_activation_receipt_digest: str
    confirmed_at_ms: int

    def __post_init__(self) -> None:
        _require(
            type(self.pair_id) is str and _PAIR_ID_RE.fullmatch(self.pair_id) is not None,
            "invalid_terminal_confirmation",
            "invalid terminal confirmation pair id",
        )
        _require(
            type(self.protocol_version) is int and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_terminal_confirmation",
            "unsupported terminal confirmation version",
        )
        _require(
            type(self.space_id) is str and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_terminal_confirmation",
            "invalid terminal confirmation space id",
        )
        for value, label in (
            (self.source_fingerprint, "source"),
            (self.target_fingerprint, "target"),
        ):
            _require(
                type(value) is str and _FINGERPRINT_RE.fullmatch(value) is not None,
                "invalid_terminal_confirmation",
                f"invalid terminal confirmation {label} fingerprint",
            )
        _require(
            type(self.base_epoch) is int and self.base_epoch >= 0,
            "invalid_terminal_confirmation",
            "invalid terminal confirmation base epoch",
        )
        _require(
            type(self.membership_epoch) is int
            and self.membership_epoch == self.base_epoch + 2,
            "invalid_terminal_confirmation",
            "invalid terminal confirmation membership epoch",
        )
        _check_digest(self.source_activation_receipt_digest, allow_empty=False)
        _check_digest(self.target_activation_receipt_digest, allow_empty=False)
        _require(
            type(self.confirmed_at_ms) is int and self.confirmed_at_ms >= 0,
            "invalid_terminal_confirmation",
            "invalid terminal confirmation timestamp",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_epoch": self.base_epoch,
            "confirmed_at_ms": self.confirmed_at_ms,
            "membership_epoch": self.membership_epoch,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "source_activation_receipt_digest": self.source_activation_receipt_digest,
            "source_fingerprint": self.source_fingerprint,
            "space_id": self.space_id,
            "target_activation_receipt_digest": self.target_activation_receipt_digest,
            "target_fingerprint": self.target_fingerprint,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TargetTerminalConfirmationReceipt":
        expected_fields = {
            "base_epoch",
            "confirmed_at_ms",
            "membership_epoch",
            "pair_id",
            "protocol_version",
            "source_activation_receipt_digest",
            "source_fingerprint",
            "space_id",
            "target_activation_receipt_digest",
            "target_fingerprint",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected_fields,
            "invalid_terminal_confirmation",
            "terminal confirmation fields are incomplete or unknown",
        )
        return cls(**dict(data))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "TargetTerminalConfirmationReceipt":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SignedTargetTerminalConfirmationReceipt:
    """Target's detached signature over a terminal all-ACK readback proof."""

    receipt: TargetTerminalConfirmationReceipt
    signature: str

    @classmethod
    def sign(
        cls, receipt: TargetTerminalConfirmationReceipt, private_key: MeshPrivateKey
    ) -> "SignedTargetTerminalConfirmationReceipt":
        signature = private_key.sign(
            _TARGET_TERMINAL_CONFIRMATION_DOMAIN + receipt.canonical_bytes()
        )
        return cls(receipt=receipt, signature=_b64url_no_pad(signature))

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid terminal-confirmation signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw,
                _TARGET_TERMINAL_CONFIRMATION_DOMAIN + self.receipt.canonical_bytes(),
            )
        except Exception as exc:  # cryptography raises InvalidSignature
            raise MeshPairingError(
                "bad_signature", "target terminal confirmation signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"receipt": self.receipt.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "SignedTargetTerminalConfirmationReceipt":
        _require(
            isinstance(data, Mapping) and set(data) == {"receipt", "signature"},
            "invalid_terminal_confirmation",
            "signed terminal confirmation is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "terminal confirmation signature is invalid"
            )
        return cls(
            receipt=TargetTerminalConfirmationReceipt.from_dict(data["receipt"]),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedTargetTerminalConfirmationReceipt":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SourceTerminalDispositionReceipt:
    """Source-signed proof that this target pairing may be torn down locally.

    A target's mutable local session cannot prove that the source never admitted
    it.  This receipt is written by the source while serialized with the source
    membership authority: either before Transition 1 (``pre_t1_cancel``), or
    immediately after its exact PENDING incarnation was evicted
    (``pending_evicted``).  It is the only terminal source evidence a target
    may use to release its own reservation/fence.
    """

    pair_id: str
    protocol_version: int
    disposition: str
    space_id: str
    source_public_key: str
    source_fingerprint: str
    target_public_key: str
    target_fingerprint: str
    invitation_digest: str
    claim_digest: str
    base_epoch: int
    membership_epoch: int
    membership_view_digest: str
    issued_at_ms: int

    def __post_init__(self) -> None:
        _require(_PAIR_ID_RE.fullmatch(self.pair_id) is not None, "invalid_terminal_disposition", "invalid terminal disposition pair id")
        _require(
            type(self.protocol_version) is int and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_terminal_disposition", "unsupported terminal disposition version",
        )
        _require(
            self.disposition in {"pre_t1_cancel", "pending_evicted"},
            "invalid_terminal_disposition", "invalid terminal disposition kind",
        )
        _check_str(self.space_id)
        for public_key, fingerprint, side in (
            (self.source_public_key, self.source_fingerprint, "source"),
            (self.target_public_key, self.target_fingerprint, "target"),
        ):
            _require(
                type(public_key) is str and _PUBLIC_KEY_RE.fullmatch(public_key) is not None,
                "invalid_terminal_disposition", f"invalid terminal disposition {side} key",
            )
            _require(
                type(fingerprint) is str and _FINGERPRINT_RE.fullmatch(fingerprint) is not None,
                "invalid_terminal_disposition", f"invalid terminal disposition {side} fingerprint",
            )
            try:
                actual = mesh_identity_fingerprint(public_key)
            except MeshIdentityError as exc:
                raise MeshPairingError(
                    "invalid_terminal_disposition", f"invalid terminal disposition {side} key"
                ) from exc
            _require(
                actual == fingerprint,
                "invalid_terminal_disposition", f"terminal disposition {side} identity mismatch",
            )
        _require(
            type(self.base_epoch) is int and self.base_epoch >= 0,
            "invalid_terminal_disposition", "invalid terminal disposition base epoch",
        )
        _require(
            type(self.membership_epoch) is int and self.membership_epoch >= 0,
            "invalid_terminal_disposition", "invalid terminal disposition membership epoch",
        )
        expected_epoch = self.base_epoch if self.disposition == "pre_t1_cancel" else self.base_epoch + 2
        _require(
            self.membership_epoch == expected_epoch,
            "invalid_terminal_disposition", "terminal disposition epoch is not adjacent",
        )
        _check_digest(self.invitation_digest, allow_empty=False)
        _check_digest(self.claim_digest, allow_empty=False)
        _require(
            self.invitation_digest != self.claim_digest,
            "invalid_terminal_disposition",
            "terminal disposition artifact digests collide",
        )
        _check_digest(self.membership_view_digest, allow_empty=False)
        _require(
            type(self.issued_at_ms) is int and self.issued_at_ms >= 0,
            "invalid_terminal_disposition", "invalid terminal disposition timestamp",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_epoch": self.base_epoch,
            "disposition": self.disposition,
            "issued_at_ms": self.issued_at_ms,
            "invitation_digest": self.invitation_digest,
            "claim_digest": self.claim_digest,
            "membership_epoch": self.membership_epoch,
            "membership_view_digest": self.membership_view_digest,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
            "target_public_key": self.target_public_key,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourceTerminalDispositionReceipt":
        expected = {
            "base_epoch", "claim_digest", "disposition", "issued_at_ms",
            "invitation_digest", "membership_epoch",
            "membership_view_digest", "pair_id", "protocol_version",
            "source_fingerprint", "source_public_key", "space_id",
            "target_fingerprint", "target_public_key",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected,
            "invalid_terminal_disposition", "terminal disposition fields are incomplete or unknown",
        )
        return cls(**dict(data))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SourceTerminalDispositionReceipt":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SignedSourceTerminalDispositionReceipt:
    """Detached source signature over a target-release disposition receipt."""

    receipt: SourceTerminalDispositionReceipt
    signature: str

    @classmethod
    def sign(
        cls, receipt: SourceTerminalDispositionReceipt, private_key: MeshPrivateKey
    ) -> "SignedSourceTerminalDispositionReceipt":
        return cls(
            receipt=receipt,
            signature=_b64url_no_pad(
                private_key.sign(_SOURCE_TERMINAL_DISPOSITION_DOMAIN + receipt.canonical_bytes())
            ),
        )

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid terminal disposition signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw, _SOURCE_TERMINAL_DISPOSITION_DOMAIN + self.receipt.canonical_bytes()
            )
        except Exception as exc:
            raise MeshPairingError(
                "bad_signature", "terminal disposition signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"receipt": self.receipt.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "SignedSourceTerminalDispositionReceipt":
        _require(
            isinstance(data, Mapping) and set(data) == {"receipt", "signature"},
            "invalid_terminal_disposition", "signed terminal disposition is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "terminal disposition signature is invalid"
            )
        return cls(
            receipt=SourceTerminalDispositionReceipt.from_dict(data["receipt"]),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedSourceTerminalDispositionReceipt":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SourcePendingEvictionIntent:
    """Source-signed precondition for a PENDING -> EVICTED removal.

    This is intentionally distinct from the terminal disposition.  It is
    persisted while the exact PENDING incarnation still exists, under the same
    locks that perform its removal.  A crash after the membership mutation can
    then recover the terminal receipt; a mutable later EVICTED view alone can
    never cause the source to mint target-release authority.
    """

    pair_id: str
    protocol_version: int
    space_id: str
    source_public_key: str
    source_fingerprint: str
    target_public_key: str
    target_fingerprint: str
    invitation_digest: str
    claim_digest: str
    base_epoch: int
    membership_epoch: int
    membership_view_digest: str
    issued_at_ms: int

    def __post_init__(self) -> None:
        _require(
            _PAIR_ID_RE.fullmatch(self.pair_id) is not None,
            "invalid_pending_eviction_intent",
            "invalid pending eviction intent pair id",
        )
        _require(
            type(self.protocol_version) is int and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_pending_eviction_intent",
            "unsupported pending eviction intent version",
        )
        _check_str(self.space_id)
        for public_key, fingerprint, side in (
            (self.source_public_key, self.source_fingerprint, "source"),
            (self.target_public_key, self.target_fingerprint, "target"),
        ):
            _require(
                type(public_key) is str and _PUBLIC_KEY_RE.fullmatch(public_key) is not None,
                "invalid_pending_eviction_intent",
                f"invalid pending eviction intent {side} key",
            )
            _require(
                type(fingerprint) is str and _FINGERPRINT_RE.fullmatch(fingerprint) is not None,
                "invalid_pending_eviction_intent",
                f"invalid pending eviction intent {side} fingerprint",
            )
            try:
                actual = mesh_identity_fingerprint(public_key)
            except MeshIdentityError as exc:
                raise MeshPairingError(
                    "invalid_pending_eviction_intent",
                    f"invalid pending eviction intent {side} key",
                ) from exc
            _require(
                actual == fingerprint,
                "invalid_pending_eviction_intent",
                f"pending eviction intent {side} identity mismatch",
            )
        _require(
            type(self.base_epoch) is int and self.base_epoch >= 0,
            "invalid_pending_eviction_intent",
            "invalid pending eviction intent base epoch",
        )
        _require(
            type(self.membership_epoch) is int
            and self.membership_epoch == self.base_epoch + 1,
            "invalid_pending_eviction_intent",
            "pending eviction intent epoch is not Transition 1",
        )
        _check_digest(self.invitation_digest, allow_empty=False)
        _check_digest(self.claim_digest, allow_empty=False)
        _require(
            self.invitation_digest != self.claim_digest,
            "invalid_pending_eviction_intent",
            "pending eviction intent artifact digests collide",
        )
        _check_digest(self.membership_view_digest, allow_empty=False)
        _require(
            type(self.issued_at_ms) is int and self.issued_at_ms >= 0,
            "invalid_pending_eviction_intent",
            "invalid pending eviction intent timestamp",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_epoch": self.base_epoch,
            "claim_digest": self.claim_digest,
            "invitation_digest": self.invitation_digest,
            "issued_at_ms": self.issued_at_ms,
            "membership_epoch": self.membership_epoch,
            "membership_view_digest": self.membership_view_digest,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
            "target_public_key": self.target_public_key,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourcePendingEvictionIntent":
        expected = {
            "base_epoch", "claim_digest", "invitation_digest", "issued_at_ms",
            "membership_epoch", "membership_view_digest", "pair_id",
            "protocol_version", "source_fingerprint", "source_public_key",
            "space_id", "target_fingerprint", "target_public_key",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected,
            "invalid_pending_eviction_intent",
            "pending eviction intent fields are incomplete or unknown",
        )
        return cls(**dict(data))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SourcePendingEvictionIntent":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SignedSourcePendingEvictionIntent:
    """Detached source signature over an exact PENDING-removal intent."""

    intent: SourcePendingEvictionIntent
    signature: str

    @classmethod
    def sign(
        cls, intent: SourcePendingEvictionIntent, private_key: MeshPrivateKey
    ) -> "SignedSourcePendingEvictionIntent":
        return cls(
            intent=intent,
            signature=_b64url_no_pad(
                private_key.sign(
                    _SOURCE_PENDING_EVICTION_INTENT_DOMAIN + intent.canonical_bytes()
                )
            ),
        )

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid pending eviction intent signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw,
                _SOURCE_PENDING_EVICTION_INTENT_DOMAIN + self.intent.canonical_bytes(),
            )
        except Exception as exc:
            raise MeshPairingError(
                "bad_signature", "pending eviction intent signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"intent": self.intent.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SignedSourcePendingEvictionIntent":
        _require(
            isinstance(data, Mapping) and set(data) == {"intent", "signature"},
            "invalid_pending_eviction_intent",
            "signed pending eviction intent is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "pending eviction intent signature is invalid"
            )
        return cls(
            intent=SourcePendingEvictionIntent.from_dict(data["intent"]),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedSourcePendingEvictionIntent":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SourcePreClaimCancelBarrier:
    """Source-signed abort authority before a target has sent its claim.

    An ISSUED source has no target identity, so it cannot yet create the normal
    target-bound terminal disposition.  This immutable barrier records the
    exact pre-T1 source view that made cancellation safe.  A later valid claim
    may consume it once to materialize the ordinary target-bound disposition;
    a mutable ``cancelled`` session alone never has that authority.
    """

    pair_id: str
    protocol_version: int
    space_id: str
    source_public_key: str
    source_fingerprint: str
    invitation_digest: str
    base_epoch: int
    membership_epoch: int
    membership_view_digest: str
    issued_at_ms: int

    def __post_init__(self) -> None:
        _require(
            _PAIR_ID_RE.fullmatch(self.pair_id) is not None,
            "invalid_preclaim_cancel_barrier",
            "invalid pre-claim cancellation barrier pair id",
        )
        _require(
            type(self.protocol_version) is int
            and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_preclaim_cancel_barrier",
            "unsupported pre-claim cancellation barrier version",
        )
        _require(
            type(self.space_id) is str and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_preclaim_cancel_barrier",
            "invalid pre-claim cancellation barrier space id",
        )
        _require(
            type(self.source_public_key) is str
            and _PUBLIC_KEY_RE.fullmatch(self.source_public_key) is not None,
            "invalid_preclaim_cancel_barrier",
            "invalid pre-claim cancellation barrier source key",
        )
        _require(
            type(self.source_fingerprint) is str
            and _FINGERPRINT_RE.fullmatch(self.source_fingerprint) is not None,
            "invalid_preclaim_cancel_barrier",
            "invalid pre-claim cancellation barrier source fingerprint",
        )
        try:
            actual = mesh_identity_fingerprint(self.source_public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_preclaim_cancel_barrier",
                "invalid pre-claim cancellation barrier source key",
            ) from exc
        _require(
            actual == self.source_fingerprint,
            "invalid_preclaim_cancel_barrier",
            "pre-claim cancellation barrier source identity mismatch",
        )
        _check_digest(self.invitation_digest, allow_empty=False)
        _require(
            type(self.base_epoch) is int and self.base_epoch >= 0,
            "invalid_preclaim_cancel_barrier",
            "invalid pre-claim cancellation barrier base epoch",
        )
        _require(
            type(self.membership_epoch) is int
            and self.membership_epoch == self.base_epoch,
            "invalid_preclaim_cancel_barrier",
            "pre-claim cancellation barrier epoch is not pre-transition",
        )
        _check_digest(self.membership_view_digest, allow_empty=False)
        _require(
            type(self.issued_at_ms) is int and self.issued_at_ms >= 0,
            "invalid_preclaim_cancel_barrier",
            "invalid pre-claim cancellation barrier timestamp",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_epoch": self.base_epoch,
            "invitation_digest": self.invitation_digest,
            "issued_at_ms": self.issued_at_ms,
            "membership_epoch": self.membership_epoch,
            "membership_view_digest": self.membership_view_digest,
            "pair_id": self.pair_id,
            "protocol_version": self.protocol_version,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SourcePreClaimCancelBarrier":
        expected = {
            "base_epoch", "invitation_digest", "issued_at_ms", "membership_epoch",
            "membership_view_digest", "pair_id", "protocol_version",
            "source_fingerprint", "source_public_key", "space_id",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected,
            "invalid_preclaim_cancel_barrier",
            "pre-claim cancellation barrier fields are incomplete or unknown",
        )
        return cls(**dict(data))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SourcePreClaimCancelBarrier":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class SignedSourcePreClaimCancelBarrier:
    """Detached source signature over an ISSUED cancellation barrier."""

    barrier: SourcePreClaimCancelBarrier
    signature: str

    @classmethod
    def sign(
        cls, barrier: SourcePreClaimCancelBarrier, private_key: MeshPrivateKey
    ) -> "SignedSourcePreClaimCancelBarrier":
        return cls(
            barrier=barrier,
            signature=_b64url_no_pad(
                private_key.sign(
                    _SOURCE_PRECLAIM_CANCEL_BARRIER_DOMAIN
                    + barrier.canonical_bytes()
                )
            ),
        )

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid pre-claim cancellation barrier signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw,
                _SOURCE_PRECLAIM_CANCEL_BARRIER_DOMAIN
                + self.barrier.canonical_bytes(),
            )
        except Exception as exc:
            raise MeshPairingError(
                "bad_signature", "pre-claim cancellation barrier signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"barrier": self.barrier.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SignedSourcePreClaimCancelBarrier":
        _require(
            isinstance(data, Mapping) and set(data) == {"barrier", "signature"},
            "invalid_preclaim_cancel_barrier",
            "signed pre-claim cancellation barrier is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "pre-claim cancellation barrier signature is invalid"
            )
        return cls(
            barrier=SourcePreClaimCancelBarrier.from_dict(data["barrier"]),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedSourcePreClaimCancelBarrier":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class TargetPairingAdmissionAnchor:
    """Target-signed durable discriminator for a #417 target space.

    This record is intentionally *not* a pairing-tail authority: it has no
    pair id, source identity, epoch, receipt chain, or lifecycle phase.  It
    only records that this target space has entered the #417 direct-fence
    protocol, so complete loss of the mutable per-pair fence records cannot
    make the ordinary-write guard silently reinterpret it as legacy.  It is
    deliberately non-permissioning: a released historical tail can never make
    a newer held tail writable merely by replaying this anchor.
    """

    protocol_version: int
    space_id: str
    target_public_key: str
    target_fingerprint: str
    issued_at_ms: int

    def __post_init__(self) -> None:
        _require(
            type(self.protocol_version) is int
            and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_target_pairing_anchor",
            "unsupported target pairing anchor version",
        )
        _require(
            type(self.space_id) is str
            and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_target_pairing_anchor",
            "invalid target pairing anchor space id",
        )
        _require(
            type(self.target_public_key) is str
            and _PUBLIC_KEY_RE.fullmatch(self.target_public_key) is not None,
            "invalid_target_pairing_anchor",
            "invalid target pairing anchor key",
        )
        _require(
            type(self.target_fingerprint) is str
            and _FINGERPRINT_RE.fullmatch(self.target_fingerprint) is not None,
            "invalid_target_pairing_anchor",
            "invalid target pairing anchor fingerprint",
        )
        try:
            actual_fingerprint = mesh_identity_fingerprint(self.target_public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_target_pairing_anchor", "invalid target pairing anchor key"
            ) from exc
        _require(
            actual_fingerprint == self.target_fingerprint,
            "invalid_target_pairing_anchor",
            "target pairing anchor identity mismatch",
        )
        _require(
            type(self.issued_at_ms) is int and self.issued_at_ms >= 0,
            "invalid_target_pairing_anchor",
            "invalid target pairing anchor timestamp",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "issued_at_ms": self.issued_at_ms,
            "protocol_version": self.protocol_version,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
            "target_public_key": self.target_public_key,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TargetPairingAdmissionAnchor":
        expected_fields = {
            "issued_at_ms",
            "protocol_version",
            "space_id",
            "target_fingerprint",
            "target_public_key",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected_fields,
            "invalid_target_pairing_anchor",
            "target pairing anchor fields are incomplete or unknown",
        )
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class SignedTargetPairingAdmissionAnchor:
    """Detached target signature over the permanent target-space anchor."""

    anchor: TargetPairingAdmissionAnchor
    signature: str

    @classmethod
    def sign(
        cls,
        anchor: TargetPairingAdmissionAnchor,
        private_key: MeshPrivateKey,
    ) -> "SignedTargetPairingAdmissionAnchor":
        signature = private_key.sign(
            _TARGET_PAIRING_ADMISSION_ANCHOR_DOMAIN + anchor.canonical_bytes()
        )
        return cls(anchor=anchor, signature=_b64url_no_pad(signature))

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid target pairing anchor signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw,
                _TARGET_PAIRING_ADMISSION_ANCHOR_DOMAIN
                + self.anchor.canonical_bytes(),
            )
        except Exception as exc:  # cryptography raises InvalidSignature
            raise MeshPairingError(
                "bad_signature", "target pairing anchor signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"anchor": self.anchor.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "SignedTargetPairingAdmissionAnchor":
        _require(
            isinstance(data, Mapping) and set(data) == {"anchor", "signature"},
            "invalid_target_pairing_anchor",
            "signed target pairing anchor is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "target pairing anchor signature is invalid"
            )
        return cls(
            anchor=TargetPairingAdmissionAnchor.from_dict(data["anchor"]),
            signature=signature,
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedTargetPairingAdmissionAnchor":
        return cls.from_dict(canonical_loads(raw))


@dataclass(frozen=True, slots=True)
class TargetPairingFenceAuthority:
    """Target-signed, bounded authority for one target-space pairing tail.

    The ordinary-write guard cannot derive protocol truth by scanning the
    append-only local session history: historic sessions are operational
    records, and their inventory is deliberately bounded.  This authority is
    instead keyed directly by ``space_id``.  ``held`` is written before the
    target reservation; ``terminal_confirmed`` embeds the exact signed all-ACK
    chain before that raw reservation is released; ``released`` represents a
    proven pre-mutation target cancellation.  The record is never an unsigned
    session-state shortcut.
    """

    pair_id: str
    protocol_version: int
    phase: str
    space_id: str
    source_public_key: str
    source_fingerprint: str
    target_public_key: str
    target_fingerprint: str
    invitation_digest: str
    requested_scopes_digest: str
    base_epoch: int
    target_activation_receipt: Mapping[str, Any] | None
    source_activation_receipt: Mapping[str, Any] | None
    target_terminal_confirmation: Mapping[str, Any] | None
    issued_at_ms: int

    def __post_init__(self) -> None:
        _require(
            type(self.pair_id) is str
            and _PAIR_ID_RE.fullmatch(self.pair_id) is not None,
            "invalid_target_pairing_fence",
            "invalid target pairing fence pair id",
        )
        _require(
            type(self.protocol_version) is int
            and self.protocol_version == _PROTOCOL_VERSION,
            "invalid_target_pairing_fence",
            "unsupported target pairing fence version",
        )
        _require(
            type(self.phase) is str
            and self.phase in {"held", "terminal_confirmed", "released"},
            "invalid_target_pairing_fence",
            "invalid target pairing fence phase",
        )
        _require(
            type(self.space_id) is str
            and _SPACE_ID_RE.fullmatch(self.space_id) is not None,
            "invalid_target_pairing_fence",
            "invalid target pairing fence space id",
        )
        for public_key, fingerprint, label in (
            (self.source_public_key, self.source_fingerprint, "source"),
            (self.target_public_key, self.target_fingerprint, "target"),
        ):
            _require(
                type(public_key) is str
                and _PUBLIC_KEY_RE.fullmatch(public_key) is not None,
                "invalid_target_pairing_fence",
                f"invalid {label} target pairing fence key",
            )
            _require(
                type(fingerprint) is str
                and _FINGERPRINT_RE.fullmatch(fingerprint) is not None,
                "invalid_target_pairing_fence",
                f"invalid {label} target pairing fence fingerprint",
            )
            try:
                actual_fingerprint = mesh_identity_fingerprint(public_key)
            except MeshIdentityError as exc:
                raise MeshPairingError(
                    "invalid_target_pairing_fence",
                    f"invalid {label} target pairing fence key",
                ) from exc
            _require(
                actual_fingerprint == fingerprint,
                "invalid_target_pairing_fence",
                f"{label} target pairing fence identity mismatch",
            )
        _check_digest(self.invitation_digest, allow_empty=False)
        _check_digest(self.requested_scopes_digest, allow_empty=False)
        _require(
            type(self.base_epoch) is int and self.base_epoch >= 0,
            "invalid_target_pairing_fence",
            "invalid target pairing fence epoch",
        )
        _require(
            type(self.issued_at_ms) is int and self.issued_at_ms >= 0,
            "invalid_target_pairing_fence",
            "invalid target pairing fence timestamp",
        )

        records = (
            self.target_activation_receipt,
            self.source_activation_receipt,
            self.target_terminal_confirmation,
        )
        if self.phase != "terminal_confirmed":
            _require(
                records == (None, None, None),
                "invalid_target_pairing_fence",
                "non-terminal target pairing fence carries receipts",
            )
            return
        _require(
            all(record is not None for record in records),
            "invalid_target_pairing_fence",
            "terminal target pairing fence lacks receipts",
        )
        try:
            target_receipt = SignedTargetActivationReceipt.from_dict(
                self.target_activation_receipt  # type: ignore[arg-type]
            )
            source_receipt = SignedSourceActivationReceipt.from_dict(
                self.source_activation_receipt  # type: ignore[arg-type]
            )
            terminal_confirmation = SignedTargetTerminalConfirmationReceipt.from_dict(
                self.target_terminal_confirmation  # type: ignore[arg-type]
            )
            target_receipt.verify(self.target_public_key)
            source_receipt.verify(self.source_public_key)
            terminal_confirmation.verify(self.target_public_key)
        except (MeshPairingError, TypeError, ValueError) as exc:
            raise MeshPairingError(
                "invalid_target_pairing_fence",
                "terminal target pairing fence receipt is invalid",
            ) from exc
        target = target_receipt.receipt
        source = source_receipt.receipt
        terminal = terminal_confirmation.receipt
        target_digest = hashlib.sha256(target_receipt.canonical_bytes()).hexdigest()
        source_digest = hashlib.sha256(source_receipt.canonical_bytes()).hexdigest()
        if (
            target.authority.pair_id != self.pair_id
            or target.authority.space_id != self.space_id
            or target.authority.source_fingerprint != self.source_fingerprint
            or target.authority.target_fingerprint != self.target_fingerprint
            or target.authority.membership_epoch != self.base_epoch + 1
            or target.membership_epoch != self.base_epoch + 2
            or source.pair_id != self.pair_id
            or source.space_id != self.space_id
            or source.source_fingerprint != self.source_fingerprint
            or source.target_fingerprint != self.target_fingerprint
            or source.base_epoch != self.base_epoch
            or source.membership_epoch != self.base_epoch + 2
            or source.membership_view_digest != target.membership_view_digest
            or source.target_activation_receipt_digest != target_digest
            or SignedTargetActivationReceipt.from_dict(
                source.target_activation_receipt
            ).canonical_bytes()
            != target_receipt.canonical_bytes()
            or terminal.pair_id != self.pair_id
            or terminal.space_id != self.space_id
            or terminal.source_fingerprint != self.source_fingerprint
            or terminal.target_fingerprint != self.target_fingerprint
            or terminal.base_epoch != self.base_epoch
            or terminal.membership_epoch != self.base_epoch + 2
            or terminal.target_activation_receipt_digest != target_digest
            or terminal.source_activation_receipt_digest != source_digest
        ):
            raise MeshPairingError(
                "invalid_target_pairing_fence",
                "terminal target pairing fence receipt bindings are invalid",
            )
        object.__setattr__(
            self, "target_activation_receipt", target_receipt.as_dict()
        )
        object.__setattr__(
            self, "source_activation_receipt", source_receipt.as_dict()
        )
        object.__setattr__(
            self,
            "target_terminal_confirmation",
            terminal_confirmation.as_dict(),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "base_epoch": self.base_epoch,
            "invitation_digest": self.invitation_digest,
            "issued_at_ms": self.issued_at_ms,
            "pair_id": self.pair_id,
            "phase": self.phase,
            "protocol_version": self.protocol_version,
            "requested_scopes_digest": self.requested_scopes_digest,
            "source_activation_receipt": (
                None
                if self.source_activation_receipt is None
                else dict(self.source_activation_receipt)
            ),
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "target_activation_receipt": (
                None
                if self.target_activation_receipt is None
                else dict(self.target_activation_receipt)
            ),
            "target_fingerprint": self.target_fingerprint,
            "target_public_key": self.target_public_key,
            "target_terminal_confirmation": (
                None
                if self.target_terminal_confirmation is None
                else dict(self.target_terminal_confirmation)
            ),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TargetPairingFenceAuthority":
        expected_fields = {
            "base_epoch",
            "invitation_digest",
            "issued_at_ms",
            "pair_id",
            "phase",
            "protocol_version",
            "requested_scopes_digest",
            "source_activation_receipt",
            "source_fingerprint",
            "source_public_key",
            "space_id",
            "target_activation_receipt",
            "target_fingerprint",
            "target_public_key",
            "target_terminal_confirmation",
        }
        _require(
            isinstance(data, Mapping) and set(data) == expected_fields,
            "invalid_target_pairing_fence",
            "target pairing fence fields are incomplete or unknown",
        )
        return cls(**dict(data))


@dataclass(frozen=True, slots=True)
class SignedTargetPairingFenceAuthority:
    """Detached target signature over a bounded target pairing fence."""

    authority: TargetPairingFenceAuthority
    signature: str

    @classmethod
    def sign(
        cls,
        authority: TargetPairingFenceAuthority,
        private_key: MeshPrivateKey,
    ) -> "SignedTargetPairingFenceAuthority":
        signature = private_key.sign(
            _TARGET_PAIRING_FENCE_DOMAIN + authority.canonical_bytes()
        )
        return cls(authority=authority, signature=_b64url_no_pad(signature))

    def verify(self, public_key: str) -> None:
        try:
            verifier = parse_mesh_public_key(public_key)
        except MeshIdentityError as exc:
            raise MeshPairingError(
                "invalid_key", "invalid target pairing fence signer key"
            ) from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(
                raw, _TARGET_PAIRING_FENCE_DOMAIN + self.authority.canonical_bytes()
            )
        except Exception as exc:  # cryptography raises InvalidSignature
            raise MeshPairingError(
                "bad_signature", "target pairing fence signature is invalid"
            ) from exc

    def as_dict(self) -> dict[str, Any]:
        return {"authority": self.authority.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any]
    ) -> "SignedTargetPairingFenceAuthority":
        _require(
            isinstance(data, Mapping) and set(data) == {"authority", "signature"},
            "invalid_target_pairing_fence",
            "signed target pairing fence is malformed",
        )
        signature = data["signature"]
        if type(signature) is not str:
            raise MeshPairingError(
                "invalid_signature", "target pairing fence signature is invalid"
            )
        return cls(
            authority=TargetPairingFenceAuthority.from_dict(data["authority"]),
            signature=signature,
        )


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
    "SourcePreparationState",
    "SourcePreparationIntent",
    "SourceActivationMigrationAuthority",
    "SignedSourceActivationMigrationAuthority",
    "SourceBootstrapEvidence",
    "SignedSourceBootstrapEvidence",
    "ImportValidatedAuthority",
    "TargetActivationReceipt",
    "SignedTargetActivationReceipt",
    "SourceActivationReceipt",
    "SignedSourceActivationReceipt",
    "TargetTerminalConfirmationReceipt",
    "SignedTargetTerminalConfirmationReceipt",
    "SourceTerminalDispositionReceipt",
    "SignedSourceTerminalDispositionReceipt",
    "SourcePendingEvictionIntent",
    "SignedSourcePendingEvictionIntent",
    "SourcePreClaimCancelBarrier",
    "SignedSourcePreClaimCancelBarrier",
    "TargetPairingAdmissionAnchor",
    "SignedTargetPairingAdmissionAnchor",
    "TargetPairingFenceAuthority",
    "SignedTargetPairingFenceAuthority",
    "BlockedRecoveryEvidence",
    "SignedBlockedRecoveryEvidence",
    "PRE_MUTATION_STATES",
    "TERMINAL_STATES",
]
