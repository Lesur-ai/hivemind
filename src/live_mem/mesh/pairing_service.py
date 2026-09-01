# -*- coding: utf-8 -*-
"""Durable Project Mesh pairing orchestration (P10-3, issue #191).

Ties the P10-3 pieces into the two-administrator, three-action flow and the
peer-route handlers the ``MeshNamespaceRouter`` delegates to:

* **Create** (source): issue a signed one-time invitation.
* **Accept** (target): reserve the blank target, send a signed join claim.
* **Approve** (source): admit the target ``pending`` (Transition 1, e -> e+1),
  export a bounded signed bootstrap, and — after the target's final ACK — promote
  it ``active`` (Transition 2, e+1 -> e+2) and deliver the signed activation.

Membership advances only through the existing membership authority
(``MembershipService``); the applied ``MembershipView`` stays the runtime
authority (ADR-0024).  The target self-applies its own e+2 activation through the
confined router branch (:meth:`try_pending_self_activation`) because it is the
one node that cannot self-apply (PENDING, no local authority).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from ..core.hivemind import (
    BankCommit,
    BankVersionPointer,
    BootstrapError,
    BootstrapLimitError,
    BootstrapService,
    CorruptedStateError,
    EventEnvelope,
    EventType,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipView,
    MembershipEpochError,
    MembershipIncarnationError,
    MembershipService,
    NodeHealth,
    NodeIdentity,
    TermState,
    TokenLeaseState,
    TokenState,
    active_members,
    hive_status_label,
    layout,
    token_mutation_lock,
)
from ..core.hivemind.models import PeerScope
from ..core.consolidation_queue import ConsolidationQueueService, get_consolidation_queue
from ..core.locks import get_lock_manager
from ..core.reservation_guard import PairingActivationError, source_preparation_key
from ..core.space import SpaceService
from .artifacts import (
    MESH_INVITATION_TTL_MILLISECONDS,
    MESH_TARGET_UNBOUND,
    MeshArtifactError,
    MeshArtifactKind,
    MeshEnrollmentApproval,
    MeshInvitation,
    MeshJoinClaim,
    SignedMeshArtifact,
    verify_approval_authority,
    verify_artifact_chain,
)
from .bootstrap_snapshot import (
    MeshBootstrapError,
    SignedMeshBootstrapEnvelope,
    build_bootstrap,
    import_bootstrap,
    parse_snapshot_payload,
    payload_digest,
    serialize_snapshot,
)
from .canonical import canonical_dumps, canonical_loads
from .destination import (
    MeshDestination,
    MeshDestinationError,
    is_public_mesh_address,
)
from .identity import (
    decode_membership_public_key,
    decode_mesh_public_key,
    mesh_identity_fingerprint,
)
from .membership_sync import candidate_view_digest, projected_promotion_view
from .pairing_client import MeshPairingClient, PeerSender
from .pairing_state import (
    PRE_MUTATION_STATES,
    BlockedRecoveryEvidence,
    ImportValidatedAuthority,
    MeshPairingRole,
    MeshPairingSession,
    MeshPairingState,
    SignedSourceActivationReceipt,
    SignedSourceActivationMigrationAuthority,
    SignedBlockedRecoveryEvidence,
    SignedSourceBootstrapEvidence,
    SignedSourcePendingEvictionIntent,
    SignedSourcePreClaimCancelBarrier,
    SignedSourceTerminalDispositionReceipt,
    SourceActivationReceipt,
    SourceActivationMigrationAuthority,
    SignedTargetActivationReceipt,
    SignedTargetPairingAdmissionAnchor,
    SignedTargetPairingFenceAuthority,
    SignedTargetTerminalConfirmationReceipt,
    SourceBootstrapEvidence,
    SourcePreparationIntent,
    SourcePreparationState,
    SourcePendingEvictionIntent,
    SourcePreClaimCancelBarrier,
    SourceTerminalDispositionReceipt,
    TargetActivationReceipt,
    TargetPairingAdmissionAnchor,
    TargetPairingFenceAuthority,
    TargetTerminalConfirmationReceipt,
)
from .pairing_store import (
    MAX_PAIRING_SESSIONS,
    MAX_TARGET_ACCEPTANCE_INTENTS_MIGRATION,
    MeshPairingStore,
    MeshPairingStoreError,
)
from .secret import (
    generate_invitation_secret,
    generate_pair_id,
    generate_pairing_nonce,
    generate_request_id,
    hash_invitation_secret,
    verify_invitation_secret,
)
from .wire import (
    MeshHttpOperation,
    MeshRequestEnvelope,
    MeshResponseCode,
    MeshResponseEnvelope,
)


class MeshPairingServiceError(RuntimeError):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


_SOURCE_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)
_PAIR_ID_RE = re.compile(r"^pair_[0-9a-f]{32}$", re.ASCII)
_SOURCE_INITIALIZATION_REASON = "source_initialization"
_SOURCE_READY_REASON = "source_ready"
_SOURCE_MUTATING_STATES = frozenset(
    {
        MeshPairingState.TRANSFERRING.value,
        MeshPairingState.AWAITING_ACKS.value,
        MeshPairingState.BLOCKED_RECOVERY.value,
    }
)
_STATE_TOKEN_VERSION = 2
_STATUS_MAX_SPACES = 128
# Diagnostic/admin history is deliberately a small bounded slice so the status
# response remains below the global response cap even when every public session
# field is near its maximum length.  Authority fallbacks must remain exhaustive
# over the larger persisted-session bound and therefore use a separate limit.
_STATUS_MAX_SESSIONS = 48
_PREPARATION_MAX_HIVEMIND_KEYS = 6
_STATUS_KNOWN_SYSTEM_PREFIXES = 2
_READINESS_MAX_MEMBERS_BYTES = 262_144
_READINESS_MAX_SINGLETON_BYTES = 65_536
_READINESS_MAX_PRODUCT_BYTES = 262_144
_READINESS_MAX_PREPARATION_BYTES = 65_536


def _framed_record(*parts: bytes) -> bytes:
    """Unambiguous length-framed record for state-token hashing."""

    out = bytearray()
    for part in parts:
        out.extend(len(part).to_bytes(8, "big"))
        out.extend(part)
    return bytes(out)


def _framed_digest(domain: str, records) -> str:
    """Hash a variable record stream without passing arrays through HCJ."""

    digest = hashlib.sha256()
    digest.update(_framed_record(b"mesh-source-state-v2", domain.encode("ascii")))
    for record in records:
        digest.update(_framed_record(record))
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """A signed-response spec the router emits: a code + an exact body."""

    code: MeshResponseCode
    body: bytes


def _ok(payload: dict[str, Any]) -> HandlerResult:
    return HandlerResult(MeshResponseCode.OK, canonical_dumps(payload))


def _refuse(code: MeshResponseCode) -> HandlerResult:
    return HandlerResult(code, canonical_dumps({"code": code.value}))


class MeshPairingService:
    """Per-instance Project Mesh pairing orchestration (source + target)."""

    STATUS_MAX_SPACES = _STATUS_MAX_SPACES
    STATUS_MAX_SESSIONS = _STATUS_MAX_SESSIONS

    def __init__(
        self,
        config,
        storage=None,
        *,
        clock_ms: Callable[[], int],
        sender_factory: Callable[[str], PeerSender],
        storage_factory: Optional[Callable[[], object]] = None,
        nonce_factory: Callable[[], str] = generate_pairing_nonce,
        pair_id_factory: Callable[[], str] = generate_pair_id,
        secret_factory: Callable[[], str] = generate_invitation_secret,
        request_id_factory: Callable[[], str] = generate_request_id,
        consolidation_queue: Optional[ConsolidationQueueService] = None,
    ) -> None:
        self._config = config
        # Storage is resolved lazily (like the router's storage_factory) so
        # constructing the service never forces backend connection at app-build
        # time. Tests may pass a concrete ``storage`` instance instead.
        if storage_factory is None:
            storage_factory = lambda: storage  # noqa: E731
        self._storage_factory = storage_factory
        self._clock_ms = clock_ms
        self._sender_factory = sender_factory
        self._nonce_factory = nonce_factory
        self._pair_id_factory = pair_id_factory
        self._secret_factory = secret_factory
        self._request_id_factory = request_id_factory
        self._consolidation_queue = (
            consolidation_queue
            if consolidation_queue is not None
            else get_consolidation_queue()
        )
        self._store_cache: Optional[MeshPairingStore] = None

    # -- infrastructure ----------------------------------------------------

    @property
    def store(self) -> MeshPairingStore:
        if self._store_cache is None:
            self._store_cache = MeshPairingStore(
                self._storage_factory(),
                prefix=f"_system/mesh_pairing/{self._config.fingerprint}/",
            )
        return self._store_cache

    def _hive_store(self, space_id: str) -> HivemindStateStore:
        return HivemindStateStore(storage=self._storage_factory(), space_id=space_id)

    def _membership(self, space_id: str) -> MembershipService:
        return MembershipService(self._hive_store(space_id))

    def _bootstrap(self) -> BootstrapService:
        return BootstrapService(self._storage_factory())

    async def _block_recovery(
        self,
        session: MeshPairingSession,
        *,
        phase: str,
        next_action: str,
        manifest_digest: str = "",
        candidate_view_digest: str = "",
        activation_event_id: str = "",
    ) -> MeshPairingSession:
        """Persist a post-mutation failure as ``blocked_recovery`` with SIGNED
        evidence (never a silent rollback). The evidence carries only ids/digests/
        epoch, is signed by the local instance key, and is verified on resume."""

        now = self._clock_ms()
        evidence = BlockedRecoveryEvidence(
            pair_id=session.pair_id,
            space_id=session.space_id,
            epoch=session.base_epoch + 1,
            phase=phase,
            next_action=next_action,
            manifest_digest=manifest_digest or session.bootstrap_manifest_digest,
            candidate_view_digest=candidate_view_digest,
            activation_event_id=activation_event_id or session.activation_event_id,
            issued_at_ms=now,
        )
        signed = SignedBlockedRecoveryEvidence.sign(evidence, self._config.private_key)
        await self.store.put_evidence(session.pair_id, signed)
        # A later, stronger failure can discover that a previously resumable
        # blocked pairing is actually evict-only (for example, source snapshot
        # authority changed while a process was down before Transition 2).
        # Refresh the signed diagnostic without attempting the illegal
        # BLOCKED_RECOVERY -> BLOCKED_RECOVERY transition.
        if session.state == MeshPairingState.BLOCKED_RECOVERY.value:
            blocked = session.with_fields(now_ms=now, last_error=phase)
        else:
            blocked = session.transition(
                MeshPairingState.BLOCKED_RECOVERY, now_ms=now, last_error=phase
            )
        await self.store.put_session(blocked)
        return blocked

    async def _verified_blocked_evidence(self, session: MeshPairingSession) -> SignedBlockedRecoveryEvidence:
        """Load + verify the signed blocked-recovery evidence before recovering."""

        signed = await self.store.get_evidence(session.pair_id)
        if signed is None:
            raise MeshPairingServiceError("no_evidence", "blocked recovery has no signed evidence")
        signed.verify(self._config.public_key)
        ev = signed.evidence
        if ev.pair_id != session.pair_id or ev.space_id != session.space_id:
            raise MeshPairingServiceError("bad_evidence", "blocked-recovery evidence binding mismatch")
        return signed

    def _verify_source_response(self, resp, *, source_fingerprint: str, correlation_id: str) -> bytes:
        """Verify a peer's signed wire response envelope before trusting its body.

        Binds the response to the expected source signer, this instance as the
        addressed target, the request correlation id, and the exact body digest
        (defense-in-depth beyond the payload-level signed artifacts).
        """

        try:
            envelope, signature = MeshResponseEnvelope.from_headers(resp.headers)
            envelope.verify(signature)
            envelope.bind_response(status=resp.status_code, body=resp.body)
        except Exception as exc:
            raise MeshPairingServiceError("bad_response", "peer response is not verifiable") from exc
        if (
            envelope.source_fingerprint != source_fingerprint
            or envelope.target_fingerprint != self._config.fingerprint
            or envelope.correlation_id != correlation_id
        ):
            raise MeshPairingServiceError("bad_response", "peer response binding mismatch")
        return resp.body

    def _client(self, peer_endpoint: str) -> MeshPairingClient:
        return MeshPairingClient(
            self._sender_factory(peer_endpoint),
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            private_key=self._config.private_key,
            clock_ms=self._clock_ms,
            nonce_factory=self._nonce_factory,
            request_id_factory=self._request_id_factory,
        )

    @staticmethod
    def _canonical_peer_endpoint(endpoint: str, *, peer: str = "source") -> str:
        """Validate a pairing transport origin before persisting or using it.

        The regular HTTP transport repeats resolver-time SSRF checks.  Pairing
        also accepts injected in-process senders, so this is the durable
        admission boundary for endpoints carried in invitations and claims.
        """

        if peer not in {"source", "target", "local"}:
            raise ValueError("invalid pairing endpoint peer")
        code = f"invalid_{peer}_endpoint"
        message = (
            "this instance's configured public URL is not a valid Mesh origin"
            if peer == "local"
            else f"{peer} endpoint is invalid"
        )
        try:
            destination = MeshDestination.parse(endpoint)
        except MeshDestinationError as exc:
            raise MeshPairingServiceError(code, message) from exc
        if destination.literal_ip is not None and not is_public_mesh_address(
            destination.literal_ip
        ):
            raise MeshPairingServiceError(code, message)
        return destination.canonical_url

    @staticmethod
    def _canonical_model_digest(model: Any) -> str:
        """Digest a validated protocol model in canonical persisted form.

        Hivemind authority models are Pydantic models, while pairing's durable
        records are strict frozen dataclasses with ``as_dict()``.  Both methods
        expose their validated canonical representation; reaching for an
        object's ``__dict__`` would accidentally turn an implementation detail
        into protocol authority.
        """

        if hasattr(model, "model_dump"):
            data = model.model_dump(mode="json")
        elif hasattr(model, "as_dict"):
            data = model.as_dict()
        else:
            raise TypeError("protocol model does not expose canonical data")
        return hashlib.sha256(canonical_dumps(data)).hexdigest()

    @classmethod
    def _bootstrap_membership_digest(cls, membership: MembershipView) -> str:
        """Digest membership as it appears in a bootstrap snapshot.

        Source-only incarnation tags deliberately do not travel to the target;
        every other field (including endpoint, display name, scopes, and the
        timestamp) remains binding state and must match exactly.
        """

        exported = membership.model_copy(
            update={
                "members": [
                    member.model_copy(update={"incarnation": None})
                    for member in membership.members
                ]
            }
        )
        return cls._canonical_model_digest(exported)

    @classmethod
    def _activation_membership_digest(cls, membership: MembershipView) -> str:
        """Digest an e+2 projection while excluding only local apply time.

        ``MembershipView.updated_at`` is assigned independently when each peer
        applies the same membership event, so it cannot be part of cross-peer
        e+2 equality.  Unlike ``candidate_view_digest`` this keeps endpoint,
        display name, joined time, scopes, and every other routing-relevant
        member field binding.  Incarnation remains source-local bootstrap
        metadata and is excluded for the same reason as in the e+1 snapshot.
        """

        projected = membership.model_copy(
            update={
                "updated_at": "",
                "members": [
                    member.model_copy(update={"incarnation": None})
                    for member in membership.members
                ],
            }
        )
        return cls._canonical_model_digest(projected)

    @classmethod
    def _token_evidence_fields(
        cls, token: TokenLeaseState | None
    ) -> tuple[str, int, int, int, int, str]:
        """Return the full, canonical token observation used by activation fences."""

        if token is None:
            return (
                "absent",
                0,
                0,
                0,
                0,
                hashlib.sha256(b"mesh-token-absent-v1\0").hexdigest(),
            )
        state = (
            token.state.value if isinstance(token.state, TokenState) else str(token.state)
        )
        return (
            state,
            token.term,
            token.fencing_token,
            token.membership_epoch,
            token.bank_version,
            cls._canonical_model_digest(token),
        )

    async def _source_enrollment_approval(
        self, session: MeshPairingSession
    ) -> MeshEnrollmentApproval:
        """Load the source's immutable, signed target binding.

        ``MeshPairingSession`` is deliberately operational state and can be
        changed only by local persistence.  It must therefore never be the sole
        authority for a target's wire identity: an ACK/status/bootstrap caller is
        authorized by the signed enrollment approval that admitted its pending
        member, not by mutable session key/fingerprint fields.
        """

        raw = await self.store.get_blob(session.pair_id, "approval")
        if raw is None:
            raise MeshPairingServiceError(
                "missing_artifacts", "pairing approval is unavailable"
            )
        signed = SignedMeshArtifact.from_bytes(raw)
        signed.verify()
        approval = signed.artifact
        if (
            not isinstance(approval, MeshEnrollmentApproval)
            or approval.pair_id != session.pair_id
            or approval.space_id != session.space_id
            or approval.membership_epoch != session.base_epoch
            or approval.source_public_key != self._config.public_key
            or approval.source_fingerprint != self._config.fingerprint
        ):
            raise MeshPairingServiceError(
                "bad_binding", "pairing approval does not match source authority"
            )
        return approval

    async def _target_enrollment_approval(
        self, session: MeshPairingSession
    ) -> MeshEnrollmentApproval:
        """Load the target's retained, signed invitation/claim/approval chain.

        This binds a post-import activation to the source identity originally
        invited by this target.  A mutable session, import marker, or retained
        bootstrap blob cannot substitute a second valid Mesh signer.
        """

        invitation_raw, claim_raw, approval_raw = await asyncio.gather(
            self.store.get_blob(session.pair_id, "invitation"),
            self.store.get_blob(session.pair_id, "claim"),
            self.store.get_blob(session.pair_id, "validated_approval"),
        )
        if invitation_raw is None or claim_raw is None or approval_raw is None:
            raise MeshPairingServiceError(
                "missing_artifacts", "target enrollment authority is unavailable"
            )
        invitation = SignedMeshArtifact.from_bytes(invitation_raw)
        claim = SignedMeshArtifact.from_bytes(claim_raw)
        approval = SignedMeshArtifact.from_bytes(approval_raw)
        verify_artifact_chain(invitation, claim, approval)
        artifact = approval.artifact
        if (
            not isinstance(artifact, MeshEnrollmentApproval)
            or artifact.pair_id != session.pair_id
            or artifact.space_id != session.space_id
            or artifact.membership_epoch != session.base_epoch
            or artifact.target_public_key != self._config.public_key
            or artifact.target_fingerprint != self._config.fingerprint
        ):
            raise MeshPairingServiceError(
                "bad_binding", "target enrollment authority does not match pairing"
            )
        return artifact

    async def _signed_pairing_binding(
        self, session: MeshPairingSession
    ) -> MeshEnrollmentApproval:
        """Return the signed chain root appropriate for this local pairing role."""

        if session.role == MeshPairingRole.SOURCE.value:
            return await self._source_enrollment_approval(session)
        if session.role == MeshPairingRole.TARGET.value:
            return await self._target_enrollment_approval(session)
        raise MeshPairingServiceError("bad_binding", "pairing role is invalid")

    async def _source_request_is_enrolled_target(
        self, session: MeshPairingSession, envelope: MeshRequestEnvelope
    ) -> bool:
        """Check a source-side peer request against immutable target authority."""

        try:
            approval = await self._source_enrollment_approval(session)
            return (
                envelope.source_public_key == approval.target_public_key
                and envelope.source_fingerprint == approval.target_fingerprint
                and session.target_public_key == approval.target_public_key
                and session.target_fingerprint == approval.target_fingerprint
            )
        except Exception:
            return False

    async def _terminal_disposition_artifact_digests(
        self, session: MeshPairingSession
    ) -> tuple[str, str]:
        """Return the exact signed invitation/claim roots for a disposition.

        A source disposition is deliberately useful before Transition 1, when
        no enrollment approval exists.  Binding it to the two independently
        signed artifacts prevents a mutable operational session from choosing
        a different target identity for a real source signature.
        """

        invitation_raw, claim_raw = await asyncio.gather(
            self.store.get_blob(session.pair_id, "invitation"),
            self.store.get_blob(session.pair_id, "claim"),
        )
        if invitation_raw is None or claim_raw is None:
            raise MeshPairingServiceError(
                "missing_artifacts",
                "terminal disposition enrollment authority is unavailable",
            )
        try:
            invitation = SignedMeshArtifact.from_bytes(invitation_raw)
            claim = SignedMeshArtifact.from_bytes(claim_raw)
            invitation.verify()
            claim.verify()
        except Exception as exc:
            raise MeshPairingServiceError(
                "bad_binding",
                "terminal disposition enrollment authority is invalid",
            ) from exc
        invitation_artifact = invitation.artifact
        claim_artifact = claim.artifact
        if (
            not isinstance(invitation_artifact, MeshInvitation)
            or not isinstance(claim_artifact, MeshJoinClaim)
            or invitation_artifact.pair_id != session.pair_id
            or claim_artifact.pair_id != session.pair_id
            or invitation_artifact.space_id != session.space_id
            or claim_artifact.space_id != session.space_id
            or invitation_artifact.membership_epoch != session.base_epoch
            or claim_artifact.membership_epoch != session.base_epoch
            or invitation_artifact.source_public_key != session.source_public_key
            or invitation_artifact.source_fingerprint != session.source_fingerprint
            or claim_artifact.source_public_key != session.source_public_key
            or claim_artifact.source_fingerprint != session.source_fingerprint
            or claim_artifact.target_public_key != session.target_public_key
            or claim_artifact.target_fingerprint != session.target_fingerprint
            or claim_artifact.invitation_digest != invitation.digest()
        ):
            raise MeshPairingServiceError(
                "bad_binding",
                "terminal disposition enrollment authority does not match pairing",
            )
        return invitation.digest(), claim.digest()

    async def _source_invitation_secret_matches_session(
        self, session: MeshPairingSession
    ) -> bool:
        """Bind the mutable source secret digest to its signed invitation.

        A secret burn is useful recovery evidence only if it names the exact
        secret the source originally issued.  Session JSON is operational
        state, so it cannot supply that digest by itself.
        """

        try:
            if session.role != MeshPairingRole.SOURCE.value:
                return False
            raw = await self.store.get_blob(session.pair_id, "invitation")
            if raw is None:
                return False
            signed = SignedMeshArtifact.from_bytes(raw)
            signed.verify()
            invitation = signed.artifact
            return (
                isinstance(invitation, MeshInvitation)
                and invitation.pair_id == session.pair_id
                and invitation.space_id == session.space_id
                and invitation.membership_epoch == session.base_epoch
                and invitation.source_public_key == session.source_public_key
                and invitation.source_fingerprint == session.source_fingerprint
                and signed.digest() == session.invitation_digest
                and invitation.secret_digest == session.secret_digest
            )
        except Exception:
            return False

    async def _source_terminal_disposition_matches_session(
        self,
        signed: SignedSourceTerminalDispositionReceipt,
        session: MeshPairingSession,
    ) -> bool:
        """Verify a locally retained source disposition against source authority."""

        try:
            signed.verify(self._config.public_key)
            receipt = signed.receipt
            invitation_digest, claim_digest = (
                await self._terminal_disposition_artifact_digests(session)
            )
            return (
                session.role == MeshPairingRole.SOURCE.value
                and receipt.pair_id == session.pair_id
                and receipt.space_id == session.space_id
                and receipt.base_epoch == session.base_epoch
                and receipt.source_public_key == self._config.public_key
                and receipt.source_fingerprint == self._config.fingerprint
                and session.source_public_key == self._config.public_key
                and session.source_fingerprint == self._config.fingerprint
                and receipt.target_public_key == session.target_public_key
                and receipt.target_fingerprint == session.target_fingerprint
                and receipt.invitation_digest == invitation_digest
                and receipt.claim_digest == claim_digest
            )
        except Exception:
            return False

    async def _target_terminal_disposition_matches_session(
        self,
        signed: SignedSourceTerminalDispositionReceipt,
        session: MeshPairingSession,
    ) -> bool:
        """Verify a source disposition before target teardown/release."""

        try:
            signed.verify(session.source_public_key)
            receipt = signed.receipt
            invitation_digest, claim_digest = (
                await self._terminal_disposition_artifact_digests(session)
            )
            return (
                session.role == MeshPairingRole.TARGET.value
                and receipt.pair_id == session.pair_id
                and receipt.space_id == session.space_id
                and receipt.base_epoch == session.base_epoch
                and receipt.source_public_key == session.source_public_key
                and receipt.source_fingerprint == session.source_fingerprint
                and receipt.target_public_key == self._config.public_key
                and receipt.target_fingerprint == self._config.fingerprint
                and session.target_public_key == self._config.public_key
                and session.target_fingerprint == self._config.fingerprint
                and receipt.invitation_digest == invitation_digest
                and receipt.claim_digest == claim_digest
            )
        except Exception:
            return False

    async def _source_pending_eviction_intent_matches_session(
        self,
        signed: SignedSourcePendingEvictionIntent,
        session: MeshPairingSession,
    ) -> bool:
        """Verify the pre-removal source authority independent of mutable state."""

        try:
            signed.verify(self._config.public_key)
            intent = signed.intent
            invitation_digest, claim_digest = (
                await self._terminal_disposition_artifact_digests(session)
            )
            return (
                session.role == MeshPairingRole.SOURCE.value
                and intent.pair_id == session.pair_id
                and intent.space_id == session.space_id
                and intent.base_epoch == session.base_epoch
                and intent.source_public_key == self._config.public_key
                and intent.source_fingerprint == self._config.fingerprint
                and session.source_public_key == self._config.public_key
                and session.source_fingerprint == self._config.fingerprint
                and intent.target_public_key == session.target_public_key
                and intent.target_fingerprint == session.target_fingerprint
                and intent.invitation_digest == invitation_digest
                and intent.claim_digest == claim_digest
            )
        except Exception:
            return False

    async def _source_pending_eviction_intent_matches_membership(
        self,
        signed: SignedSourcePendingEvictionIntent,
        session: MeshPairingSession,
        membership: MembershipView,
    ) -> bool:
        if not await self._source_pending_eviction_intent_matches_session(
            signed, session
        ):
            return False
        intent = signed.intent
        target_node_id = _node_id_from_fingerprint(intent.target_fingerprint)
        target_key = _legacy_membership_key(intent.target_public_key)
        return (
            membership.epoch == intent.membership_epoch
            and self._canonical_model_digest(membership)
            == intent.membership_view_digest
            and any(
                member.node_id == target_node_id
                and member.public_key == target_key
                and member.status == MemberStatus.PENDING.value
                and member.incarnation == intent.pair_id
                for member in membership.members
            )
        )

    async def _source_preclaim_cancel_barrier_matches(
        self,
        signed: SignedSourcePreClaimCancelBarrier,
        session: MeshPairingSession,
        membership: MembershipView,
    ) -> bool:
        """Verify an ISSUED abort without trusting the mutable session state."""

        try:
            signed.verify(self._config.public_key)
            barrier = signed.barrier
            return (
                session.role == MeshPairingRole.SOURCE.value
                and barrier.pair_id == session.pair_id
                and barrier.space_id == session.space_id
                and barrier.base_epoch == session.base_epoch
                and barrier.membership_epoch == membership.epoch
                and barrier.membership_epoch == session.base_epoch
                and barrier.membership_view_digest
                == self._canonical_model_digest(membership)
                and barrier.source_public_key == self._config.public_key
                and barrier.source_fingerprint == self._config.fingerprint
                and session.source_public_key == self._config.public_key
                and session.source_fingerprint == self._config.fingerprint
                and barrier.invitation_digest == session.invitation_digest
            )
        except Exception:
            return False

    async def _persist_source_preclaim_cancel_barrier(
        self, session: MeshPairingSession, membership: MembershipView
    ) -> SignedSourcePreClaimCancelBarrier:
        """Persist/readback the source abort before ISSUED becomes CANCELLED."""

        if (
            session.role != MeshPairingRole.SOURCE.value
            or session.state != MeshPairingState.ISSUED.value
            or session.target_public_key
            or session.target_fingerprint
            or session.claim_digest
            or membership.epoch != session.base_epoch
        ):
            raise MeshPairingServiceError(
                "cancel_unproven",
                "source pre-claim cancellation does not match its issued pairing",
            )
        barrier = SourcePreClaimCancelBarrier(
            pair_id=session.pair_id,
            protocol_version=1,
            space_id=session.space_id,
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            invitation_digest=session.invitation_digest,
            base_epoch=session.base_epoch,
            membership_epoch=membership.epoch,
            membership_view_digest=self._canonical_model_digest(membership),
            issued_at_ms=self._clock_ms(),
        )
        signed = SignedSourcePreClaimCancelBarrier.sign(
            barrier, self._config.private_key
        )
        await self.store.put_source_preclaim_cancel_barrier(signed)
        persisted = await self.store.get_source_preclaim_cancel_barrier(
            session.pair_id
        )
        if persisted is None or not await self._source_preclaim_cancel_barrier_matches(
            persisted, session, membership
        ):
            raise MeshPairingServiceError(
                "cancel_unproven",
                "source pre-claim cancellation barrier did not persist safely",
            )
        return persisted

    @staticmethod
    def _preclaim_cancelled_session_updates() -> dict[str, Any]:
        """Clear mutable admission residue under a verified ISSUED abort."""

        return {
            "target_public_key": "",
            "target_fingerprint": "",
            "target_endpoint": "",
            "claim_digest": "",
            "approval_digest": "",
            "bootstrap_manifest_digest": "",
            "bootstrap_bank_version": -1,
            "activation_event_id": "",
            "last_error": "",
        }

    async def _persist_source_pending_eviction_intent(
        self, session: MeshPairingSession, membership: MembershipView
    ) -> SignedSourcePendingEvictionIntent:
        """Write/readback the exact e+1 PENDING removal intent under lock."""

        target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
        target_key = _legacy_membership_key(session.target_public_key)
        if (
            session.role != MeshPairingRole.SOURCE.value
            or membership.epoch != session.base_epoch + 1
            or not any(
                member.node_id == target_node_id
                and member.public_key == target_key
                and member.status == MemberStatus.PENDING.value
                and member.incarnation == session.pair_id
                for member in membership.members
            )
        ):
            raise MeshPairingServiceError(
                "eviction_unproven",
                "source pending eviction does not match this pairing",
            )
        invitation_digest, claim_digest = await self._terminal_disposition_artifact_digests(
            session
        )
        intent = SourcePendingEvictionIntent(
            pair_id=session.pair_id,
            protocol_version=1,
            space_id=session.space_id,
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            target_public_key=session.target_public_key,
            target_fingerprint=session.target_fingerprint,
            invitation_digest=invitation_digest,
            claim_digest=claim_digest,
            base_epoch=session.base_epoch,
            membership_epoch=membership.epoch,
            membership_view_digest=self._canonical_model_digest(membership),
            issued_at_ms=self._clock_ms(),
        )
        signed = SignedSourcePendingEvictionIntent.sign(
            intent, self._config.private_key
        )
        await self.store.put_source_pending_eviction_intent(signed)
        persisted = await self.store.get_source_pending_eviction_intent(session.pair_id)
        if (
            persisted is None
            or not await self._source_pending_eviction_intent_matches_membership(
                persisted, session, membership
            )
        ):
            raise MeshPairingServiceError(
                "eviction_unproven",
                "source pending eviction intent did not persist safely",
            )
        return persisted

    async def _build_source_terminal_disposition(
        self,
        session: MeshPairingSession,
        *,
        disposition: str,
        membership: MembershipView,
    ) -> SignedSourceTerminalDispositionReceipt:
        """Build a source-signed target-release proof under source authority."""

        if session.role != MeshPairingRole.SOURCE.value:
            raise MeshPairingServiceError(
                "bad_binding", "terminal disposition requires a source pairing"
            )
        target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
        target_key = _legacy_membership_key(session.target_public_key)
        target_members = [
            member
            for member in membership.members
            if member.node_id == target_node_id and member.public_key == target_key
        ]
        if any(
            member.status in (MemberStatus.PENDING.value, MemberStatus.ACTIVE.value)
            for member in target_members
        ):
            raise MeshPairingServiceError(
                "already_admitted",
                "source membership still contains a live target pairing",
            )
        if disposition == "pre_t1_cancel":
            if membership.epoch != session.base_epoch:
                raise MeshPairingServiceError(
                    "epoch_changed",
                    "source membership changed before cancellation",
                )
        elif disposition == "pending_evicted":
            if membership.epoch != session.base_epoch + 2 or not any(
                member.status == MemberStatus.EVICTED.value
                and member.incarnation == session.pair_id
                for member in target_members
            ):
                raise MeshPairingServiceError(
                    "eviction_unproven",
                    "source pending eviction is not adjacent to this pairing",
                )
        else:
            raise MeshPairingServiceError(
                "invalid_terminal_disposition",
                "terminal disposition kind is invalid",
            )
        invitation_digest, claim_digest = await self._terminal_disposition_artifact_digests(
            session
        )
        receipt = SourceTerminalDispositionReceipt(
            pair_id=session.pair_id,
            protocol_version=1,
            disposition=disposition,
            space_id=session.space_id,
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            target_public_key=session.target_public_key,
            target_fingerprint=session.target_fingerprint,
            invitation_digest=invitation_digest,
            claim_digest=claim_digest,
            base_epoch=session.base_epoch,
            membership_epoch=membership.epoch,
            membership_view_digest=self._canonical_model_digest(membership),
            issued_at_ms=self._clock_ms(),
        )
        return SignedSourceTerminalDispositionReceipt.sign(
            receipt, self._config.private_key
        )

    async def _persist_source_terminal_disposition(
        self,
        session: MeshPairingSession,
        *,
        disposition: str,
        membership: MembershipView,
    ) -> SignedSourceTerminalDispositionReceipt:
        signed = await self._build_source_terminal_disposition(
            session, disposition=disposition, membership=membership
        )
        await self.store.put_source_terminal_disposition(signed)
        persisted = await self.store.get_source_terminal_disposition(session.pair_id)
        if (
            persisted is None
            or not await self._source_terminal_disposition_matches_session(
                persisted, session
            )
            or persisted.receipt.disposition != disposition
        ):
            raise MeshPairingServiceError(
                "terminal_disposition_unavailable",
                "source terminal disposition did not persist safely",
            )
        return persisted

    async def _source_terminal_disposition_for_status(
        self,
        session: MeshPairingSession,
        envelope: MeshRequestEnvelope,
    ) -> SignedSourceTerminalDispositionReceipt | None:
        """Return the currently safe, target-bound terminal disposition.

        The signature proves the source's exact pre-T1 cancellation or adjacent
        PENDING eviction.  The live membership check prevents a historical
        disposition from authorizing teardown after the same identity was
        re-admitted under a newer pairing incarnation.
        """

        try:
            signed = await self.store.get_source_terminal_disposition(
                session.pair_id
            )
            if (
                signed is None
                or not await self._source_terminal_disposition_matches_session(
                    signed, session
                )
            ):
                return None
            receipt = signed.receipt
            if (
                envelope.space_id != receipt.space_id
                or envelope.membership_epoch != receipt.base_epoch
                or envelope.source_public_key != receipt.target_public_key
                or envelope.source_fingerprint != receipt.target_fingerprint
            ):
                return None
            membership = await self._hive_store(session.space_id).get_membership()
            if membership is None or membership.epoch < receipt.membership_epoch:
                return None
            target_node_id = _node_id_from_fingerprint(receipt.target_fingerprint)
            target_key = _legacy_membership_key(receipt.target_public_key)
            target_members = [
                member
                for member in membership.members
                if member.node_id == target_node_id and member.public_key == target_key
            ]
            if any(
                member.status
                in (MemberStatus.PENDING.value, MemberStatus.ACTIVE.value)
                for member in target_members
            ):
                return None
            if receipt.disposition == "pending_evicted" and not any(
                member.status == MemberStatus.EVICTED.value
                and member.incarnation == receipt.pair_id
                for member in target_members
            ):
                return None
            return signed
        except Exception:
            return None

    async def _source_preparation_health_matches_evidence(
        self,
        session: MeshPairingSession,
        health: NodeHealth | None,
        preparation: SourcePreparationIntent | None,
    ) -> bool:
        """Bind source health/provenance to the already-exported e+1 snapshot.

        A preparation record makes ``node_status.json`` mandatory.  More
        importantly, the original export records whether this source was a
        prepared or legacy source and the exact health marker it served from;
        deleting both records later must not resurrect the legacy no-marker
        compatibility path for a pending e+1 pairing.
        """

        try:
            signed = await self.store.get_source_bootstrap_evidence(session.pair_id)
            if signed is None:
                # No #417 evidence exists only for a legacy in-memory route;
                # keep the old no-preparation/no-marker representation narrow.
                return preparation is None and health is None
            signed.verify(self._config.public_key)
            evidence = signed.evidence
            expected_preparation = (
                ""
                if preparation is None
                else self._canonical_model_digest(preparation)
            )
            expected_health = (
                "" if health is None else self._canonical_model_digest(health)
            )
            return (
                evidence.space_id == session.space_id
                and evidence.source_fingerprint == session.source_fingerprint
                and evidence.target_fingerprint == session.target_fingerprint
                and evidence.preparation_digest == expected_preparation
                and evidence.health_digest == expected_health
                and (
                    preparation is None
                    or (
                        preparation.state_enum is SourcePreparationState.COMPLETE
                        and self._preparation_binding_matches(
                            session.space_id, preparation
                        )
                    )
                )
            )
        except Exception:
            return False

    async def _source_is_healthy_for_bootstrap(
        self, session: MeshPairingSession
    ) -> bool:
        """Require the live source to remain an eligible active commit member.

        Legacy committed spaces can legitimately predate both the explicit
        source-preparation workflow and ``node_status.json``; in that narrow
        representation structural identity/membership authority is the health
        proof.  Once a preparation intent exists, absence of the marker is
        corruption, not legacy compatibility: preparation writes HEALTHY last
        and a missing/corrupt marker must fence every bootstrap/activation tail.
        """

        try:
            store = self._hive_store(session.space_id)
            node, membership, health, preparation = await asyncio.gather(
                store.get_node_identity(),
                store.get_membership(),
                store.get_node_status(),
                self.store.get_source_preparation(session.space_id),
            )
            if (
                node is None
                or membership is None
                or (health is None and preparation is not None)
                or (
                    health is not None
                    and health.status != HiveNodeStatus.HEALTHY.value
                )
            ):
                return False
            configured = decode_membership_public_key(self._config.public_key)
            node_key = decode_membership_public_key(node.public_key)
            source_member = next(
                (
                    member
                    for member in membership.members
                    if member.node_id == node.node_id
                    and member.status == MemberStatus.ACTIVE.value
                ),
                None,
            )
            return (
                source_member is not None
                and source_member.has_scope(PeerScope.COMMIT)
                and decode_membership_public_key(source_member.public_key)
                == node_key
                and node_key == configured
                and await self._source_preparation_health_matches_evidence(
                    session, health, preparation
                )
            )
        except Exception:
            return False

    async def _source_health_marker_allows_bootstrap(
        self, session: MeshPairingSession
    ) -> bool:
        """Keep explicit unsafe/resync markers from serving a snapshot.

        A full current-member predicate is sent in authenticated status metadata
        for destructive target resync preflight.  Ordinary first import may still
        obtain the already-signed e+1 snapshot when a source's later scope drift
        will be caught by the source-side final fence, preserving non-destructive
        recovery of a pending target.  Missing status remains compatible only
        with a truly legacy source that has no preparation intent.
        """

        try:
            health, preparation = await asyncio.gather(
                self._hive_store(session.space_id).get_node_status(),
                self.store.get_source_preparation(session.space_id),
            )
            return (
                (health is None and preparation is None)
                or (health is not None and health.status == HiveNodeStatus.HEALTHY.value)
            ) and await self._source_preparation_health_matches_evidence(
                session, health, preparation
            )
        except Exception:
            return False

    async def _retained_bootstrap_snapshot(
        self,
        session: MeshPairingSession,
        *,
        envelope_blob: str,
        payload_blob: str,
    ) -> tuple[SignedMeshBootstrapEnvelope, Any]:
        """Load and verify one durable copy of the source-signed e+1 snapshot."""

        signed_raw, payload = await asyncio.gather(
            self.store.get_blob(session.pair_id, envelope_blob),
            self.store.get_blob(session.pair_id, payload_blob),
        )
        if signed_raw is None or payload is None:
            raise MeshPairingServiceError(
                "bootstrap_authority_missing", "bootstrap authority is unavailable"
            )
        approval = await self._signed_pairing_binding(session)
        signed_env = SignedMeshBootstrapEnvelope.from_bytes(signed_raw)
        signed_env.verify()
        env = signed_env.envelope
        if (
            session.source_public_key != approval.source_public_key
            or session.source_fingerprint != approval.source_fingerprint
            or session.target_public_key != approval.target_public_key
            or session.target_fingerprint != approval.target_fingerprint
            or env.source_public_key != approval.source_public_key
            or env.source_fingerprint != approval.source_fingerprint
            or env.target_fingerprint != approval.target_fingerprint
            or env.space_id != session.space_id
            or env.membership_epoch != approval.membership_epoch + 1
            or env.bank_version != session.bootstrap_bank_version
            or env.manifest_digest != session.bootstrap_manifest_digest
            or payload_digest(payload) != env.payload_digest
        ):
            raise MeshPairingServiceError(
                "bootstrap_authority_mismatch", "bootstrap authority does not match pairing"
            )
        snapshot = parse_snapshot_payload(
            payload,
            max_objects=self._config.bootstrap_max_objects,
            max_bytes=self._config.bootstrap_max_bytes,
        )
        manifest = snapshot.manifest
        if (
            manifest.manifest_sha256 != env.manifest_digest
            or manifest.membership_epoch != env.membership_epoch
            or manifest.bank_version != env.bank_version
            or (manifest.bank_version == -1 and manifest.commit_id != "")
        ):
            raise MeshPairingServiceError(
                "bootstrap_authority_mismatch", "bootstrap manifest does not match envelope"
            )
        return signed_env, snapshot

    def _bootstrap_snapshot_authority_models(
        self, snapshot: Any, *, space_id: str
    ) -> tuple[
        MembershipView,
        TermState,
        TokenLeaseState | None,
        BankVersionPointer,
        BankCommit | None,
    ]:
        """Parse the signed snapshot's exact shared state authority."""

        files = snapshot.files
        members_raw = files.get("_hivemind/members.json")
        term_raw = files.get("_hivemind/term.json")
        pointer_raw = files.get("_hivemind/bank_version.json")
        if members_raw is None or term_raw is None or pointer_raw is None:
            raise MeshPairingServiceError(
                "bootstrap_authority_mismatch", "bootstrap lacks required shared authority"
            )
        membership = MembershipView.model_validate_json(members_raw)
        term = TermState.model_validate_json(term_raw)
        token_raw = files.get("_hivemind/token.json")
        token = (
            None if token_raw is None else TokenLeaseState.model_validate_json(token_raw)
        )
        pointer = BankVersionPointer.model_validate_json(pointer_raw)
        manifest = snapshot.manifest
        if (
            membership.epoch != manifest.membership_epoch
            or pointer.bank_version != manifest.bank_version
            or pointer.commit_id != manifest.commit_id
        ):
            raise MeshPairingServiceError(
                "bootstrap_authority_mismatch", "bootstrap head does not match manifest"
            )
        commit: BankCommit | None = None
        if manifest.bank_version >= 0:
            commit_path = layout.commit_key(space_id, manifest.bank_version)[
                len(space_id) + 1 :
            ]
            commit_raw = files.get(commit_path)
            if commit_raw is None:
                raise MeshPairingServiceError(
                    "bootstrap_authority_mismatch", "bootstrap lacks selected commit"
                )
            commit = BankCommit.model_validate_json(commit_raw)
            if (
                commit.bank_version != manifest.bank_version
                or commit.commit_id != manifest.commit_id
            ):
                raise MeshPairingServiceError(
                    "bootstrap_authority_mismatch",
                    "bootstrap selected commit does not match manifest",
                )
        return membership, term, token, pointer, commit

    async def _capture_source_bootstrap_evidence(
        self,
        session: MeshPairingSession,
        *,
        membership_epoch: int,
        manifest_digest: str,
        bank_version: int,
        commit_id: str,
        recorded_at_ms: int,
    ) -> SourceBootstrapEvidence:
        """Read the exact source snapshot authority under caller-held locks."""

        store = self._hive_store(session.space_id)
        node, membership, term, token, pointer, health, preparation = await asyncio.gather(
            store.get_node_identity(),
            store.get_membership(),
            store.get_term(),
            store.get_token(),
            store.get_bank_version_pointer(),
            store.get_node_status(),
            self.store.get_source_preparation(session.space_id),
        )
        source_member = (
            None
            if membership is None or node is None
            else next(
                (
                    member
                    for member in membership.members
                    if member.node_id == node.node_id
                    and member.status == MemberStatus.ACTIVE.value
                ),
                None,
            )
        )
        if (
            node is None
            or membership is None
            or term is None
            or pointer is None
            or membership.epoch != membership_epoch
            or pointer.bank_version != bank_version
            or pointer.commit_id != commit_id
            or source_member is None
            or source_member.public_key != node.public_key
            or node.public_key != _legacy_membership_key(self._config.public_key)
            or (
                preparation is not None
                and (
                    preparation.state_enum is not SourcePreparationState.COMPLETE
                    or health is None
                    or health.status != HiveNodeStatus.HEALTHY.value
                )
            )
            or (
                health is not None
                and health.status != HiveNodeStatus.HEALTHY.value
            )
        ):
            raise MeshPairingServiceError(
                "source_snapshot_changed", "source snapshot authority changed"
            )
        selected_commit_digest = ""
        if bank_version >= 0:
            commit = await store.get_commit(bank_version)
            if (
                commit is None
                or commit.bank_version != bank_version
                or commit.commit_id != commit_id
            ):
                raise MeshPairingServiceError(
                    "source_snapshot_changed", "source snapshot authority changed"
                )
            selected_commit_digest = self._canonical_model_digest(commit)
        elif commit_id:
            raise MeshPairingServiceError(
                "source_snapshot_changed", "source snapshot authority changed"
            )
        (
            token_state,
            token_term,
            token_fencing_token,
            token_membership_epoch,
            token_bank_version,
            token_digest,
        ) = self._token_evidence_fields(token)
        return SourceBootstrapEvidence(
            pair_id=session.pair_id,
            protocol_version=1,
            space_id=session.space_id,
            source_fingerprint=session.source_fingerprint,
            target_fingerprint=session.target_fingerprint,
            membership_epoch=membership_epoch,
            membership_snapshot_digest=self._bootstrap_membership_digest(membership),
            membership_view_digest=candidate_view_digest(membership),
            manifest_digest=manifest_digest,
            bank_version=bank_version,
            commit_id=commit_id,
            node_digest=self._canonical_model_digest(node),
            term=term.term,
            term_digest=self._canonical_model_digest(term),
            token_state=token_state,
            token_term=token_term,
            token_fencing_token=token_fencing_token,
            token_membership_epoch=token_membership_epoch,
            token_bank_version=token_bank_version,
            token_digest=token_digest,
            pointer_bank_version=pointer.bank_version,
            pointer_commit_id=pointer.commit_id,
            pointer_digest=self._canonical_model_digest(pointer),
            selected_commit_digest=selected_commit_digest,
            preparation_digest=(
                ""
                if preparation is None
                else self._canonical_model_digest(preparation)
            ),
            health_digest=(
                "" if health is None else self._canonical_model_digest(health)
            ),
            recorded_at_ms=recorded_at_ms,
        )

    async def _source_bootstrap_evidence_matches(
        self, session: MeshPairingSession
    ) -> bool:
        """Require the signed export and live source to match exactly.

        The local evidence is immutable operational authority, but the signed
        bootstrap is the cryptographic binding to what the target actually
        imported.  Re-reading both prevents a same-id commit rewrite (or a
        corrupted local evidence rewrite) from authorizing e+2 against bytes
        different from the signed e+1 snapshot.
        """

        try:
            signed_evidence = await self.store.get_source_bootstrap_evidence(
                session.pair_id
            )
            if signed_evidence is None:
                return False
            signed_evidence.verify(self._config.public_key)
            evidence = signed_evidence.evidence
            if (
                evidence.space_id != session.space_id
                or evidence.source_fingerprint != session.source_fingerprint
                or evidence.target_fingerprint != session.target_fingerprint
                or evidence.membership_epoch != session.base_epoch + 1
                or evidence.manifest_digest != session.bootstrap_manifest_digest
                or evidence.bank_version != session.bootstrap_bank_version
            ):
                return False
            _signed_env, snapshot = await self._retained_bootstrap_snapshot(
                session,
                envelope_blob="bootstrap_envelope",
                payload_blob="bootstrap_payload",
            )
            membership, term, token, pointer, commit = (
                self._bootstrap_snapshot_authority_models(
                    snapshot, space_id=session.space_id
                )
            )
            snapshot_token = self._token_evidence_fields(token)
            if (
                snapshot.manifest.commit_id != evidence.commit_id
                or self._bootstrap_membership_digest(membership)
                != evidence.membership_snapshot_digest
                or candidate_view_digest(membership)
                != evidence.membership_view_digest
                or self._canonical_model_digest(term) != evidence.term_digest
                or term.term != evidence.term
                or snapshot_token
                != (
                    evidence.token_state,
                    evidence.token_term,
                    evidence.token_fencing_token,
                    evidence.token_membership_epoch,
                    evidence.token_bank_version,
                    evidence.token_digest,
                )
                or self._canonical_model_digest(pointer) != evidence.pointer_digest
                or pointer.bank_version != evidence.pointer_bank_version
                or pointer.commit_id != evidence.pointer_commit_id
            ):
                return False
            if evidence.bank_version >= 0:
                if (
                    commit is None
                    or self._canonical_model_digest(commit)
                    != evidence.selected_commit_digest
                ):
                    return False
            elif commit is not None or evidence.commit_id or evidence.selected_commit_digest:
                return False
            live = await self._capture_source_bootstrap_evidence(
                session,
                membership_epoch=evidence.membership_epoch,
                manifest_digest=evidence.manifest_digest,
                bank_version=evidence.bank_version,
                commit_id=evidence.commit_id,
                recorded_at_ms=evidence.recorded_at_ms,
            )
            return live == evidence
        except Exception:
            # At the ACK boundary an unavailable/corrupt local authority cannot
            # be guessed.  The caller makes this a durable evict-only recovery.
            return False

    async def _source_terminal_activation_state_matches(
        self, session: MeshPairingSession, event: EventEnvelope
    ) -> bool:
        """Prove the source's protected e+2 state before it signs all-ACK tail.

        The ordinary e+1 snapshot matcher intentionally cannot be reused here:
        membership has legitimately advanced to e+2.  Every *other* exported
        critical-state binding remains frozen by the source activation fence,
        and the live membership must be the exact e+2 projection of the signed
        export.  This is the shared boundary before a source signature may let
        the target release its last ordinary-write reservation.
        """

        try:
            if (
                session.role != MeshPairingRole.SOURCE.value
                or event.membership_epoch != session.base_epoch + 2
                or event.type != EventType.MEMBERSHIP_UPDATED
            ):
                return False
            signed_evidence = await self.store.get_source_bootstrap_evidence(
                session.pair_id
            )
            if signed_evidence is None:
                return False
            signed_evidence.verify(self._config.public_key)
            evidence = signed_evidence.evidence
            if (
                evidence.space_id != session.space_id
                or evidence.source_fingerprint != session.source_fingerprint
                or evidence.target_fingerprint != session.target_fingerprint
                or evidence.membership_epoch != session.base_epoch + 1
                or evidence.manifest_digest != session.bootstrap_manifest_digest
                or evidence.bank_version != session.bootstrap_bank_version
            ):
                return False
            _signed_env, snapshot = await self._retained_bootstrap_snapshot(
                session,
                envelope_blob="bootstrap_envelope",
                payload_blob="bootstrap_payload",
            )
            snapshot_membership, snapshot_term, snapshot_token, snapshot_pointer, snapshot_commit = (
                self._bootstrap_snapshot_authority_models(
                    snapshot, space_id=session.space_id
                )
            )
            snapshot_token_fields = self._token_evidence_fields(snapshot_token)
            if (
                snapshot.manifest.commit_id != evidence.commit_id
                or self._bootstrap_membership_digest(snapshot_membership)
                != evidence.membership_snapshot_digest
                or candidate_view_digest(snapshot_membership)
                != evidence.membership_view_digest
                or self._canonical_model_digest(snapshot_term) != evidence.term_digest
                or snapshot_term.term != evidence.term
                or snapshot_token_fields
                != (
                    evidence.token_state,
                    evidence.token_term,
                    evidence.token_fencing_token,
                    evidence.token_membership_epoch,
                    evidence.token_bank_version,
                    evidence.token_digest,
                )
                or self._canonical_model_digest(snapshot_pointer)
                != evidence.pointer_digest
                or snapshot_pointer.bank_version != evidence.pointer_bank_version
                or snapshot_pointer.commit_id != evidence.pointer_commit_id
            ):
                return False
            if evidence.bank_version >= 0:
                if (
                    snapshot_commit is None
                    or self._canonical_model_digest(snapshot_commit)
                    != evidence.selected_commit_digest
                ):
                    return False
            elif snapshot_commit is not None or evidence.commit_id or evidence.selected_commit_digest:
                return False

            store = self._hive_store(session.space_id)
            node, membership, term, token, pointer, health, preparation = await asyncio.gather(
                store.get_node_identity(),
                store.get_membership(),
                store.get_term(),
                store.get_token(),
                store.get_bank_version_pointer(),
                store.get_node_status(),
                self.store.get_source_preparation(session.space_id),
            )
            if node is None or membership is None or term is None or pointer is None:
                return False
            target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
            expected = projected_promotion_view(snapshot_membership, target_node_id)
            source_member = next(
                (member for member in membership.members if member.node_id == node.node_id),
                None,
            )
            target_member = next(
                (
                    member
                    for member in membership.members
                    if member.node_id == target_node_id
                ),
                None,
            )
            if (
                node.public_key != _legacy_membership_key(self._config.public_key)
                or self._canonical_model_digest(node) != evidence.node_digest
                or source_member is None
                or source_member.status != MemberStatus.ACTIVE.value
                or source_member.public_key != node.public_key
                or target_member is None
                or target_member.status != MemberStatus.ACTIVE.value
                or membership.epoch != session.base_epoch + 2
                or self._activation_membership_digest(membership)
                != self._activation_membership_digest(expected)
                or candidate_view_digest(membership)
                != event.payload.get("candidate_view_digest")
                or event.origin_node_id != node.node_id
                or event.payload.get("node_id") != target_node_id
                or event.payload.get("pair_id") != session.pair_id
                or event.payload.get("epoch") != membership.epoch
                or event.payload.get("status") != MemberStatus.ACTIVE.value
                or self._canonical_model_digest(term) != evidence.term_digest
                or self._token_evidence_fields(token)[-1] != evidence.token_digest
                or self._canonical_model_digest(pointer) != evidence.pointer_digest
                or pointer.bank_version != evidence.pointer_bank_version
                or pointer.commit_id != evidence.pointer_commit_id
                or not await self._source_preparation_health_matches_evidence(
                    session, health, preparation
                )
            ):
                return False
            if evidence.bank_version >= 0:
                commit = await store.get_commit(evidence.bank_version)
                if (
                    commit is None
                    or commit.bank_version != evidence.bank_version
                    or commit.commit_id != evidence.commit_id
                    or self._canonical_model_digest(commit)
                    != evidence.selected_commit_digest
                ):
                    return False
            return True
        except Exception:
            return False

    async def _persist_source_activation_receipt(
        self,
        session: MeshPairingSession,
        event: EventEnvelope,
        *,
        target_receipt: SignedTargetActivationReceipt,
    ) -> SignedSourceActivationReceipt:
        """Write/read-back the source's signed terminal all-ACK proof."""

        target_receipt_digest = hashlib.sha256(
            target_receipt.canonical_bytes()
        ).hexdigest()
        if (
            session.state != MeshPairingState.ACTIVE.value
            or not await self._source_terminal_activation_state_matches(session, event)
        ):
            raise MeshPairingServiceError(
                "source_terminal_confirmation_failed",
                "source terminal activation authority is unavailable",
            )
        receipt = SourceActivationReceipt(
            pair_id=session.pair_id,
            protocol_version=1,
            space_id=session.space_id,
            source_fingerprint=session.source_fingerprint,
            target_fingerprint=session.target_fingerprint,
            source_node_id=event.origin_node_id,
            target_node_id=_node_id_from_fingerprint(session.target_fingerprint),
            base_epoch=session.base_epoch,
            membership_epoch=session.base_epoch + 2,
            activation_event_id=event.event_id,
            membership_view_digest=event.payload["candidate_view_digest"],
            target_activation_receipt_digest=target_receipt_digest,
            target_activation_receipt=target_receipt.as_dict(),
            confirmed_at_ms=self._clock_ms(),
        )
        signed = SignedSourceActivationReceipt.sign(receipt, self._config.private_key)
        await self.store.put_source_activation_receipt(signed)
        stored = await self.store.get_source_activation_receipt(session.pair_id)
        if stored is None:
            raise MeshPairingServiceError(
                "source_terminal_confirmation_failed",
                "source terminal activation receipt did not read back",
            )
        stored.verify(self._config.public_key)
        stored_data = stored.receipt.as_dict()
        receipt_data = receipt.as_dict()
        stored_data.pop("confirmed_at_ms")
        receipt_data.pop("confirmed_at_ms")
        if stored_data != receipt_data:
            raise MeshPairingServiceError(
                "source_terminal_confirmation_failed",
                "source terminal activation receipt conflicts",
            )
        return stored

    @staticmethod
    def _event_with_source_activation_receipt(
        event: EventEnvelope,
        signed: SignedSourceActivationReceipt,
        request_id: str,
        *,
        terminal_confirmation: SignedTargetTerminalConfirmationReceipt | None = None,
    ) -> EventEnvelope:
        payload = dict(event.payload)
        payload["source_activation_receipt"] = signed.as_dict()
        if terminal_confirmation is not None:
            payload["target_terminal_confirmation"] = terminal_confirmation.as_dict()
        return event.model_copy(
            update={"request_id": request_id, "payload": payload}
        )

    async def _target_terminal_confirmation_for_event(
        self,
        session: MeshPairingSession,
        signed_source: SignedSourceActivationReceipt,
        event: EventEnvelope,
        *,
        base: int,
    ) -> SignedTargetTerminalConfirmationReceipt | None:
        """Parse the target's old terminal readback carried on a replay.

        This field is absent from the original source-terminal delivery: the
        target has not signed it yet.  It is present only when a source that
        already completed all-ACK replays the exact terminal event to repair a
        target that lost its local terminal triplet after normal work advanced.
        """

        try:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            raw = payload.get("target_terminal_confirmation")
            if not isinstance(raw, Mapping):
                return None
            signed = SignedTargetTerminalConfirmationReceipt.from_dict(raw)
            signed.verify(self._config.public_key)
            receipt = signed.receipt
            embedded = SignedTargetActivationReceipt.from_dict(
                signed_source.receipt.target_activation_receipt
            )
            embedded.verify(self._config.public_key)
            if (
                receipt.pair_id != session.pair_id
                or receipt.space_id != session.space_id
                or receipt.source_fingerprint != session.source_fingerprint
                or receipt.target_fingerprint != session.target_fingerprint
                or receipt.base_epoch != base
                or receipt.membership_epoch != base + 2
                or receipt.source_activation_receipt_digest
                != hashlib.sha256(signed_source.canonical_bytes()).hexdigest()
                or receipt.target_activation_receipt_digest
                != hashlib.sha256(embedded.canonical_bytes()).hexdigest()
            ):
                return None
            return signed
        except Exception:
            return None

    async def _target_receipt_from_activation_confirmation(
        self,
        session: MeshPairingSession,
        confirmation: Mapping[str, Any],
        *,
        base: int,
    ) -> SignedTargetActivationReceipt | None:
        """Verify the exact target e+2 proof carried by a signed response."""

        try:
            raw = confirmation.get("target_activation_receipt")
            if not isinstance(raw, Mapping):
                return None
            signed = SignedTargetActivationReceipt.from_dict(raw)
            approval = await self._source_enrollment_approval(session)
            signed.verify(approval.target_public_key)
            receipt = signed.receipt
            authority = receipt.authority
            digest = hashlib.sha256(signed.canonical_bytes()).hexdigest()
            if (
                confirmation.get("target_activation_receipt_digest") != digest
                or receipt.membership_epoch != base + 2
                or authority.pair_id != session.pair_id
                or authority.space_id != session.space_id
                or authority.source_fingerprint != session.source_fingerprint
                or authority.target_fingerprint != session.target_fingerprint
                or authority.local_node_id
                != _node_id_from_fingerprint(session.target_fingerprint)
            ):
                return None
            return signed
        except Exception:
            return None

    async def _persist_target_terminal_confirmation(
        self,
        session: MeshPairingSession,
        signed_source: SignedSourceActivationReceipt,
        confirmation: Mapping[str, Any],
    ) -> bool:
        """Read back the target's signed final all-ACK acknowledgement locally."""

        try:
            raw = confirmation.get("target_terminal_confirmation")
            if not isinstance(raw, Mapping):
                return False
            signed = SignedTargetTerminalConfirmationReceipt.from_dict(raw)
            approval = await self._source_enrollment_approval(session)
            signed.verify(approval.target_public_key)
            receipt = signed.receipt
            if (
                receipt.pair_id != session.pair_id
                or receipt.space_id != session.space_id
                or receipt.source_fingerprint != session.source_fingerprint
                or receipt.target_fingerprint != session.target_fingerprint
                or receipt.base_epoch != session.base_epoch
                or receipt.membership_epoch != session.base_epoch + 2
                or receipt.source_activation_receipt_digest
                != hashlib.sha256(signed_source.canonical_bytes()).hexdigest()
                or receipt.target_activation_receipt_digest
                != signed_source.receipt.target_activation_receipt_digest
            ):
                return False
            await self.store.put_target_terminal_confirmation(signed)
            stored = await self.store.get_target_terminal_confirmation(session.pair_id)
            if stored is None:
                return False
            stored.verify(approval.target_public_key)
            stored_data = stored.receipt.as_dict()
            receipt_data = receipt.as_dict()
            stored_data.pop("confirmed_at_ms")
            receipt_data.pop("confirmed_at_ms")
            return stored_data == receipt_data
        except Exception:
            return False

    async def _restore_source_terminal_confirmation_from_target_response(
        self,
        session: MeshPairingSession,
        event: EventEnvelope,
        confirmation: Mapping[str, Any],
    ) -> bool:
        """Restore an exact completed source tail from a target-signed response.

        A source may lose either of its local terminal records after all-ACK has
        completed and normal commits have advanced the head.  It must not mint a
        timestamp-different replacement from mutable state.  Instead, a duplicate
        e+2 asks the target for its retained, signed terminal chain and restores
        those exact bytes after binding them to the known event and enrollment
        approval.  If the target cannot present the complete chain this returns
        false and the source remains write-fenced.
        """

        try:
            if (
                confirmation.get("source_terminal_confirmed") is not True
                or not isinstance(confirmation.get("source_activation_receipt"), Mapping)
                or not isinstance(
                    confirmation.get("target_terminal_confirmation"), Mapping
                )
            ):
                return False
            signed_source = SignedSourceActivationReceipt.from_dict(
                confirmation["source_activation_receipt"]
            )
            signed_source.verify(self._config.public_key)
            approval = await self._source_enrollment_approval(session)
            signed_terminal = SignedTargetTerminalConfirmationReceipt.from_dict(
                confirmation["target_terminal_confirmation"]
            )
            signed_terminal.verify(approval.target_public_key)
            source_receipt = signed_source.receipt
            embedded_target = SignedTargetActivationReceipt.from_dict(
                source_receipt.target_activation_receipt
            )
            embedded_target.verify(approval.target_public_key)
            target_digest = hashlib.sha256(
                embedded_target.canonical_bytes()
            ).hexdigest()
            source_digest = hashlib.sha256(signed_source.canonical_bytes()).hexdigest()
            terminal = signed_terminal.receipt
            expected_target_node_id = _node_id_from_fingerprint(
                session.target_fingerprint
            )
            if (
                source_receipt.pair_id != session.pair_id
                or source_receipt.space_id != session.space_id
                or source_receipt.source_fingerprint != session.source_fingerprint
                or source_receipt.target_fingerprint != session.target_fingerprint
                or source_receipt.source_node_id != event.origin_node_id
                or source_receipt.target_node_id != expected_target_node_id
                or source_receipt.base_epoch != session.base_epoch
                or source_receipt.membership_epoch != session.base_epoch + 2
                or source_receipt.activation_event_id != event.event_id
                or source_receipt.membership_view_digest
                != event.payload.get("candidate_view_digest")
                or source_receipt.target_activation_receipt_digest != target_digest
                or terminal.pair_id != session.pair_id
                or terminal.space_id != session.space_id
                or terminal.source_fingerprint != session.source_fingerprint
                or terminal.target_fingerprint != session.target_fingerprint
                or terminal.base_epoch != session.base_epoch
                or terminal.membership_epoch != session.base_epoch + 2
                or terminal.source_activation_receipt_digest != source_digest
                or terminal.target_activation_receipt_digest != target_digest
            ):
                return False
            existing_source = await self.store.get_source_activation_receipt(
                session.pair_id
            )
            if (
                existing_source is None
                or existing_source.canonical_bytes()
                != signed_source.canonical_bytes()
            ):
                # The target's signed terminal confirmation binds the exact
                # source-receipt bytes it retained.  Replace a local
                # timestamp-only retry variant with those bytes before checking
                # the digest; preserving the newer local observation would make
                # a valid all-ACK tail permanently non-convergent.
                await self.store.restore_source_activation_receipt(signed_source)
                existing_source = await self.store.get_source_activation_receipt(
                    session.pair_id
                )
            if (
                existing_source is None
                or existing_source.canonical_bytes()
                != signed_source.canonical_bytes()
            ):
                return False
            if not await self._persist_target_terminal_confirmation(
                session, signed_source, confirmation
            ):
                return False
            return await self._source_terminal_confirmation_matches(session)
        except Exception:
            return False

    async def _persist_import_validation(
        self,
        session: MeshPairingSession,
        signed_env: SignedMeshBootstrapEnvelope,
        import_result: Any,
    ) -> None:
        """Record an exact target-side import proof after authoritative readback.

        The marker alone is not protocol authority.  It is cross-checked against
        a read-back copy of the source-signed envelope/payload, then against the
        imported shared state, before an activation tail may use it.
        """

        retained_env, snapshot = await self._retained_bootstrap_snapshot(
            session,
            envelope_blob="validated_bootstrap_envelope",
            payload_blob="validated_bootstrap_payload",
        )
        if retained_env.canonical_bytes() != signed_env.canonical_bytes():
            raise MeshPairingServiceError(
                "import_validation_failed", "retained bootstrap authority differs"
            )
        env = retained_env.envelope
        (
            snapshot_membership,
            snapshot_term,
            snapshot_token,
            snapshot_pointer,
            snapshot_commit,
        ) = self._bootstrap_snapshot_authority_models(
            snapshot, space_id=session.space_id
        )
        store = self._hive_store(session.space_id)
        node, membership, term, token, pointer = await asyncio.gather(
            store.get_node_identity(),
            store.get_membership(),
            store.get_term(),
            store.get_token(),
            store.get_bank_version_pointer(),
        )
        expected_node_id = _node_id_from_fingerprint(session.target_fingerprint)
        expected_key = _legacy_membership_key(self._config.public_key)
        snapshot_target = next(
            (
                member
                for member in snapshot_membership.members
                if member.node_id == expected_node_id
                and member.status == MemberStatus.PENDING.value
            ),
            None,
        )
        if (
            node is None
            or membership is None
            or term is None
            or pointer is None
            or snapshot_target is None
            or node.node_id != expected_node_id
            or node.public_key != expected_key
            or snapshot_target.public_key != expected_key
            or membership.epoch != session.base_epoch + 1
            or self._bootstrap_membership_digest(membership)
            != self._bootstrap_membership_digest(snapshot_membership)
            or candidate_view_digest(membership)
            != candidate_view_digest(snapshot_membership)
            or self._canonical_model_digest(term)
            != self._canonical_model_digest(snapshot_term)
            or self._token_evidence_fields(token)
            != self._token_evidence_fields(snapshot_token)
            or self._canonical_model_digest(pointer)
            != self._canonical_model_digest(snapshot_pointer)
            or getattr(import_result, "target_space_id", None) != session.space_id
            or getattr(import_result, "local_node_id", None) != expected_node_id
            or getattr(import_result, "membership_epoch", None)
            != session.base_epoch + 1
            or getattr(import_result, "bank_version", None) != env.bank_version
        ):
            raise MeshPairingServiceError(
                "import_validation_failed", "import authority did not read back"
            )
        if env.bank_version >= 0:
            imported_commit_id = getattr(import_result, "commit_id", None)
            commit = await store.get_commit(env.bank_version)
            if (
                snapshot_commit is None
                or commit is None
                or commit.bank_version != env.bank_version
                or type(imported_commit_id) is not str
                or imported_commit_id != snapshot_commit.commit_id
                or commit.commit_id != snapshot_commit.commit_id
                or pointer.commit_id != snapshot_commit.commit_id
                or self._canonical_model_digest(commit)
                != self._canonical_model_digest(snapshot_commit)
            ):
                raise MeshPairingServiceError(
                    "import_validation_failed", "import authority did not read back"
                )
            selected_commit_digest = self._canonical_model_digest(snapshot_commit)
        elif (
            snapshot_commit is not None
            or pointer.commit_id
            or getattr(import_result, "commit_id", None) != ""
        ):
            raise MeshPairingServiceError(
                "import_validation_failed", "import authority did not read back"
            )
        else:
            selected_commit_digest = ""
        authority = ImportValidatedAuthority(
            pair_id=session.pair_id,
            protocol_version=1,
            space_id=session.space_id,
            source_fingerprint=session.source_fingerprint,
            target_fingerprint=session.target_fingerprint,
            local_node_id=expected_node_id,
            membership_epoch=session.base_epoch + 1,
            membership_snapshot_digest=self._bootstrap_membership_digest(
                snapshot_membership
            ),
            membership_view_digest=candidate_view_digest(snapshot_membership),
            manifest_digest=env.manifest_digest,
            bank_version=env.bank_version,
            commit_id=snapshot.manifest.commit_id,
            term_digest=self._canonical_model_digest(snapshot_term),
            token_digest=self._token_evidence_fields(snapshot_token)[-1],
            pointer_digest=self._canonical_model_digest(snapshot_pointer),
            selected_commit_digest=selected_commit_digest,
            validated_at_ms=self._clock_ms(),
        )
        existing = await self.store.get_import_validation(session.pair_id)
        if existing is not None:
            existing_data = existing.as_dict()
            expected_data = authority.as_dict()
            existing_data.pop("validated_at_ms")
            expected_data.pop("validated_at_ms")
            if existing_data != expected_data:
                raise MeshPairingServiceError(
                    "import_validation_failed", "import authority conflicts"
                )
            authority = existing
        await self.store.put_import_validation(authority)
        if await self.store.get_import_validation(session.pair_id) != authority:
            raise MeshPairingServiceError(
                "import_validation_failed", "import authority did not read back"
            )

    async def _retained_import_authority(
        self, session: MeshPairingSession, *, base: int
    ) -> tuple[
        ImportValidatedAuthority,
        MembershipView,
        TermState,
        TokenLeaseState | None,
        BankVersionPointer,
        BankCommit | None,
    ]:
        """Derive the immutable e+1 authority only from retained signed bytes.

        This intentionally does *not* inspect mutable local state.  First import
        validation adds a strict local readback separately; the same immutable
        chain is what a target terminal receipt may safely retain after e+2.
        """

        if session.base_epoch != base:
            raise MeshPairingServiceError(
                "bootstrap_authority_mismatch", "bootstrap epoch does not match pairing"
            )
        retained_env, snapshot = await self._retained_bootstrap_snapshot(
            session,
            envelope_blob="validated_bootstrap_envelope",
            payload_blob="validated_bootstrap_payload",
        )
        env = retained_env.envelope
        (
            snapshot_membership,
            snapshot_term,
            snapshot_token,
            snapshot_pointer,
            snapshot_commit,
        ) = self._bootstrap_snapshot_authority_models(
            snapshot, space_id=session.space_id
        )
        expected_node_id = _node_id_from_fingerprint(session.target_fingerprint)
        expected_key = _legacy_membership_key(self._config.public_key)
        snapshot_target = next(
            (
                member
                for member in snapshot_membership.members
                if member.node_id == expected_node_id
            ),
            None,
        )
        if (
            env.membership_epoch != base + 1
            or snapshot_membership.epoch != base + 1
            or snapshot_target is None
            or snapshot_target.status != MemberStatus.PENDING.value
            or snapshot_target.public_key != expected_key
            or snapshot_pointer.bank_version != env.bank_version
            or snapshot_pointer.commit_id != snapshot.manifest.commit_id
        ):
            raise MeshPairingServiceError(
                "bootstrap_authority_mismatch", "bootstrap target authority does not match pairing"
            )
        if env.bank_version >= 0:
            if (
                snapshot_commit is None
                or snapshot_commit.bank_version != env.bank_version
                or snapshot_commit.commit_id != snapshot.manifest.commit_id
            ):
                raise MeshPairingServiceError(
                    "bootstrap_authority_mismatch", "bootstrap selected commit is invalid"
                )
            selected_commit_digest = self._canonical_model_digest(snapshot_commit)
        elif snapshot_commit is not None or snapshot.manifest.commit_id:
            raise MeshPairingServiceError(
                "bootstrap_authority_mismatch", "bootstrap empty head is invalid"
            )
        else:
            selected_commit_digest = ""
        authority = ImportValidatedAuthority(
            pair_id=session.pair_id,
            protocol_version=1,
            space_id=session.space_id,
            source_fingerprint=session.source_fingerprint,
            target_fingerprint=session.target_fingerprint,
            local_node_id=expected_node_id,
            membership_epoch=base + 1,
            membership_snapshot_digest=self._bootstrap_membership_digest(
                snapshot_membership
            ),
            membership_view_digest=candidate_view_digest(snapshot_membership),
            manifest_digest=env.manifest_digest,
            bank_version=env.bank_version,
            commit_id=snapshot.manifest.commit_id,
            term_digest=self._canonical_model_digest(snapshot_term),
            token_digest=self._token_evidence_fields(snapshot_token)[-1],
            pointer_digest=self._canonical_model_digest(snapshot_pointer),
            selected_commit_digest=selected_commit_digest,
            validated_at_ms=self._clock_ms(),
        )
        return (
            authority,
            snapshot_membership,
            snapshot_term,
            snapshot_token,
            snapshot_pointer,
            snapshot_commit,
        )

    async def _import_authority_snapshot_matches(
        self,
        session: MeshPairingSession,
        authority: ImportValidatedAuthority,
        *,
        base: int,
    ) -> tuple[
        MembershipView,
        TermState,
        TokenLeaseState | None,
        BankVersionPointer,
        BankCommit | None,
    ] | None:
        """Bind one import authority to its retained source-signed e+1 snapshot.

        This static half is intentionally reusable by a target's *terminal* e+2
        receipt.  It proves the imported snapshot/identity chain, but does not
        freeze normal shared term/token/pointer/commit mutations that can occur
        after Transition 2 while the source is persisting its own completion
        tail.  First promotion still calls :meth:`_import_authority_matches`,
        which adds the strict live e+1/e+2 checks below.
        """

        try:
            (
                expected,
                snapshot_membership,
                snapshot_term,
                snapshot_token,
                snapshot_pointer,
                snapshot_commit,
            ) = await self._retained_import_authority(session, base=base)
            expected_data = expected.as_dict()
            authority_data = authority.as_dict()
            expected_data.pop("validated_at_ms")
            authority_data.pop("validated_at_ms")
            if expected_data != authority_data:
                return None
            return (
                snapshot_membership,
                snapshot_term,
                snapshot_token,
                snapshot_pointer,
                snapshot_commit,
            )
        except Exception:
            return None

    async def _import_authority_matches(
        self,
        session: MeshPairingSession,
        authority: ImportValidatedAuthority,
        *,
        base: int,
    ) -> bool:
        """Strictly validate an import authority before its first promotion."""

        snapshot_models = await self._import_authority_snapshot_matches(
            session, authority, base=base
        )
        if snapshot_models is None:
            return False
        snapshot_membership, _snapshot_term, _snapshot_token, _snapshot_pointer, _snapshot_commit = (
            snapshot_models
        )
        try:
            store = self._hive_store(session.space_id)
            node, membership, term, token, pointer = await asyncio.gather(
                store.get_node_identity(),
                store.get_membership(),
                store.get_term(),
                store.get_token(),
                store.get_bank_version_pointer(),
            )
            expected_node_id = authority.local_node_id
            expected_key = _legacy_membership_key(self._config.public_key)
            if (
                node is None
                or membership is None
                or term is None
                or pointer is None
                or node.node_id != expected_node_id
                or node.public_key != expected_key
                or membership.epoch not in (base + 1, base + 2)
                or self._canonical_model_digest(term) != authority.term_digest
                or self._token_evidence_fields(token)[-1] != authority.token_digest
                or self._canonical_model_digest(pointer) != authority.pointer_digest
                or pointer.bank_version != authority.bank_version
                or pointer.commit_id != authority.commit_id
            ):
                return False
            if membership.epoch == base + 1:
                if (
                    self._bootstrap_membership_digest(membership)
                    != authority.membership_snapshot_digest
                ):
                    return False
                expected_membership_digest = authority.membership_view_digest
            else:
                expected_membership_digest = candidate_view_digest(
                    projected_promotion_view(snapshot_membership, expected_node_id)
                )
            if candidate_view_digest(membership) != expected_membership_digest:
                return False
            if authority.bank_version >= 0:
                commit = await store.get_commit(authority.bank_version)
                return (
                    commit is not None
                    and commit.bank_version == authority.bank_version
                    and commit.commit_id == authority.commit_id
                    and self._canonical_model_digest(commit)
                    == authority.selected_commit_digest
                )
            return authority.commit_id == "" and authority.selected_commit_digest == ""
        except Exception:
            return False

    async def _import_validation_matches(
        self, session: MeshPairingSession, *, base: int
    ) -> bool:
        """Re-read the durable target import authority before activation tails."""

        try:
            authority = await self.store.get_import_validation(session.pair_id)
            return authority is not None and await self._import_authority_matches(
                session, authority, base=base
            )
        except Exception:
            return False

    async def _persist_target_activation_receipt(
        self, session: MeshPairingSession, *, base: int
    ) -> None:
        """Durably sign the target's exact e+2 receipt before terminal state.

        The marker is the authority for the first promotion.  A separately
        signed receipt preserves that already-proved fact across a later marker
        loss, without allowing an operational ``ACTIVE`` session to substitute
        for either the e+1 import proof or the e+2 membership view.
        """

        authority = await self.store.get_import_validation(session.pair_id)
        if authority is None or not await self._import_authority_matches(
            session, authority, base=base
        ):
            raise MeshPairingServiceError(
                "import_validation_failed", "target import authority is unavailable"
            )
        store = self._hive_store(session.space_id)
        node, membership = await asyncio.gather(
            store.get_node_identity(), store.get_membership()
        )
        if (
            node is None
            or membership is None
            or membership.epoch != base + 2
            or node.node_id != authority.local_node_id
            or not any(
                member.node_id == node.node_id
                and member.status == MemberStatus.ACTIVE.value
                for member in membership.members
            )
        ):
            raise MeshPairingServiceError(
                "import_validation_failed", "target activation receipt is not authoritative"
            )
        receipt = TargetActivationReceipt(
            authority=authority,
            membership_epoch=membership.epoch,
            membership_view_digest=candidate_view_digest(membership),
            activated_at_ms=self._clock_ms(),
        )
        signed = SignedTargetActivationReceipt.sign(
            receipt, self._config.private_key
        )
        await self.store.put_target_activation_receipt(signed)
        stored = await self.store.get_target_activation_receipt(session.pair_id)
        if (
            stored is None
            or stored.receipt.authority != authority
            or stored.receipt.membership_epoch != receipt.membership_epoch
            or stored.receipt.membership_view_digest
            != receipt.membership_view_digest
        ):
            raise MeshPairingServiceError(
                "import_validation_failed", "target activation receipt did not read back"
            )
        stored.verify(self._config.public_key)

    async def _target_terminal_view_matches(
        self,
        session: MeshPairingSession,
        authority: ImportValidatedAuthority,
        expected_membership: MembershipView,
        *,
        expected_epoch: int,
        expected_view_digest: str,
    ) -> bool:
        """Verify the immutable target portion of an exact e+2 receipt.

        The caller separately proves the full e+1 import authority (including
        term, token, pointer and selected commit) still matches.  A local
        ``BankCommit``/pointer chain is not a signed authorization for a later
        head, so it must never replace that equality check while a pairing is
        awaiting all-ACK completion.
        """

        try:
            store = self._hive_store(session.space_id)
            node, membership = await asyncio.gather(
                store.get_node_identity(),
                store.get_membership(),
            )
            if (
                node is None
                or membership is None
                or node.node_id != authority.local_node_id
                or node.public_key != _legacy_membership_key(self._config.public_key)
                or membership.epoch != expected_epoch
                or not any(
                    member.node_id == node.node_id
                    and member.status == MemberStatus.ACTIVE.value
                    for member in membership.members
                )
                or self._activation_membership_digest(membership)
                != self._activation_membership_digest(expected_membership)
                or candidate_view_digest(membership) != expected_view_digest
            ):
                return False
            return True
        except Exception:
            return False

    async def _target_activation_receipt_matches(
        self, session: MeshPairingSession, *, base: int
    ) -> bool:
        """Verify a signed terminal receipt after marker loss/corruption."""

        try:
            signed = await self.store.get_target_activation_receipt(session.pair_id)
            if signed is None:
                return False
            signed.verify(self._config.public_key)
            receipt = signed.receipt
            authority = receipt.authority
            if (
                receipt.membership_epoch != base + 2
                or authority.pair_id != session.pair_id
                or authority.space_id != session.space_id
                or authority.source_fingerprint != session.source_fingerprint
                or authority.target_fingerprint != session.target_fingerprint
            ):
                return False
            if not await self._import_authority_matches(
                session, authority, base=base
            ):
                return False
            snapshot_models = await self._import_authority_snapshot_matches(
                session, authority, base=base
            )
            if snapshot_models is None:
                return False
            snapshot_membership, _term, _token, _pointer, _commit = snapshot_models
            expected_membership = projected_promotion_view(
                snapshot_membership, authority.local_node_id
            )
            return await self._target_terminal_view_matches(
                session,
                authority,
                expected_membership,
                expected_epoch=receipt.membership_epoch,
                expected_view_digest=receipt.membership_view_digest,
            )
        except Exception:
            return False

    async def _target_finalized_activation_matches(
        self, session: MeshPairingSession, *, base: int
    ) -> bool:
        """Verify the post-all-ACK terminal proof without freezing later heads.

        This path is deliberately unavailable until the target has retained the
        source-signed receipt *and* its own signed readback confirmation and
        released the matching reservation.  Before that point callers must use
        :meth:`_target_activation_receipt_matches`, which requires exact live
        e+1 term/token/pointer/commit equality.  After final all-ACK, normal
        `BANK_COMMIT` work may legitimately advance those fields; the two
        signatures and exact e+2 membership remain the activation authority.
        """

        try:
            if (
                session.role != MeshPairingRole.TARGET.value
                or session.state != MeshPairingState.ACTIVE.value
                or await self.store.get_reservation(session.space_id) is not None
            ):
                return False
            target_signed = await self.store.get_target_activation_receipt(
                session.pair_id
            )
            source_signed = await self.store.get_source_activation_receipt(
                session.pair_id
            )
            terminal_signed = await self.store.get_target_terminal_confirmation(
                session.pair_id
            )
            if (
                target_signed is None
                or source_signed is None
                or terminal_signed is None
            ):
                return False
            target_signed.verify(self._config.public_key)
            terminal_signed.verify(self._config.public_key)
            approval = await self._target_enrollment_approval(session)
            source_signed.verify(approval.source_public_key)
            target_receipt = target_signed.receipt
            authority = target_receipt.authority
            source_receipt = source_signed.receipt
            terminal = terminal_signed.receipt
            target_digest = hashlib.sha256(target_signed.canonical_bytes()).hexdigest()
            source_digest = hashlib.sha256(source_signed.canonical_bytes()).hexdigest()
            embedded_target = SignedTargetActivationReceipt.from_dict(
                source_receipt.target_activation_receipt
            )
            embedded_target.verify(self._config.public_key)
            if (
                target_receipt.membership_epoch != base + 2
                or authority.pair_id != session.pair_id
                or authority.space_id != session.space_id
                or authority.source_fingerprint != session.source_fingerprint
                or authority.target_fingerprint != session.target_fingerprint
                or source_receipt.pair_id != session.pair_id
                or source_receipt.space_id != session.space_id
                or source_receipt.source_fingerprint != session.source_fingerprint
                or source_receipt.target_fingerprint != session.target_fingerprint
                or source_receipt.base_epoch != base
                or source_receipt.membership_epoch != base + 2
                or source_receipt.membership_view_digest
                != target_receipt.membership_view_digest
                or source_receipt.target_activation_receipt_digest != target_digest
                or embedded_target.canonical_bytes() != target_signed.canonical_bytes()
                or terminal.pair_id != session.pair_id
                or terminal.space_id != session.space_id
                or terminal.source_fingerprint != session.source_fingerprint
                or terminal.target_fingerprint != session.target_fingerprint
                or terminal.base_epoch != base
                or terminal.membership_epoch != base + 2
                or terminal.source_activation_receipt_digest != source_digest
                or terminal.target_activation_receipt_digest != target_digest
            ):
                return False
            snapshot_models = await self._import_authority_snapshot_matches(
                session, authority, base=base
            )
            if snapshot_models is None:
                return False
            snapshot_membership, _term, _token, _pointer, _commit = snapshot_models
            expected_membership = projected_promotion_view(
                snapshot_membership, authority.local_node_id
            )
            if not await self._target_terminal_view_matches(
                session,
                authority,
                expected_membership,
                expected_epoch=target_receipt.membership_epoch,
                expected_view_digest=target_receipt.membership_view_digest,
            ):
                return False
            source_member = next(
                (
                    member
                    for member in expected_membership.members
                    if member.node_id == source_receipt.source_node_id
                ),
                None,
            )
            return (
                source_member is not None
                and source_member.status == MemberStatus.ACTIVE.value
                and source_member.public_key
                == _legacy_membership_key(approval.source_public_key)
            )
        except Exception:
            return False

    async def _restore_target_activation_receipt_from_source_receipt(
        self,
        session: MeshPairingSession,
        *,
        base: int,
        signed_source: SignedSourceActivationReceipt | None = None,
        require_terminal_confirmation: bool = False,
    ) -> bool:
        """Restore only the exact target receipt embedded in a source proof.

        The source terminal record embeds the target's original detached
        signature precisely so receipt loss cannot force a timestamp-changing
        replacement.  This helper is never a generic recovery fallback: the
        source signature, pair/space/epoch chain, embedded target signature,
        and (for a completed tail) target-signed terminal confirmation must all
        agree before the immutable store can accept the exact bytes.
        """

        try:
            approval = await self._target_enrollment_approval(session)
            if signed_source is None:
                signed_source = await self.store.get_source_activation_receipt(
                    session.pair_id
                )
            if signed_source is None:
                return False
            signed_source.verify(approval.source_public_key)
            source_receipt = signed_source.receipt
            embedded = SignedTargetActivationReceipt.from_dict(
                source_receipt.target_activation_receipt
            )
            embedded.verify(self._config.public_key)
            target_receipt = embedded.receipt
            authority = target_receipt.authority
            target_digest = hashlib.sha256(embedded.canonical_bytes()).hexdigest()
            if (
                source_receipt.pair_id != session.pair_id
                or source_receipt.space_id != session.space_id
                or source_receipt.source_fingerprint != session.source_fingerprint
                or source_receipt.target_fingerprint != session.target_fingerprint
                or source_receipt.base_epoch != base
                or source_receipt.membership_epoch != base + 2
                or source_receipt.target_activation_receipt_digest != target_digest
                or target_receipt.membership_epoch != base + 2
                or authority.pair_id != session.pair_id
                or authority.space_id != session.space_id
                or authority.source_fingerprint != session.source_fingerprint
                or authority.target_fingerprint != session.target_fingerprint
            ):
                return False
            if require_terminal_confirmation:
                terminal_signed = await self.store.get_target_terminal_confirmation(
                    session.pair_id
                )
                if terminal_signed is None:
                    return False
                terminal_signed.verify(self._config.public_key)
                terminal = terminal_signed.receipt
                if (
                    terminal.pair_id != session.pair_id
                    or terminal.space_id != session.space_id
                    or terminal.source_fingerprint != session.source_fingerprint
                    or terminal.target_fingerprint != session.target_fingerprint
                    or terminal.base_epoch != base
                    or terminal.membership_epoch != base + 2
                    or terminal.source_activation_receipt_digest
                    != hashlib.sha256(signed_source.canonical_bytes()).hexdigest()
                    or terminal.target_activation_receipt_digest != target_digest
                ):
                    return False
            await self.store.put_target_activation_receipt(embedded)
            stored = await self.store.get_target_activation_receipt(session.pair_id)
            return (
                stored is not None
                and stored.canonical_bytes() == embedded.canonical_bytes()
            )
        except Exception:
            return False

    async def _source_activation_receipt_for_event(
        self,
        session: MeshPairingSession,
        envelope: MeshRequestEnvelope,
        event: EventEnvelope,
        *,
        base: int,
    ) -> SignedSourceActivationReceipt | None:
        """Parse a source terminal receipt only when it is bound to this event."""

        raw = event.payload.get("source_activation_receipt")
        if raw is None:
            return None
        try:
            signed = SignedSourceActivationReceipt.from_dict(raw)
            approval = await self._target_enrollment_approval(session)
            signed.verify(approval.source_public_key)
            receipt = signed.receipt
            target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
            if (
                envelope.source_public_key != approval.source_public_key
                or envelope.source_fingerprint != approval.source_fingerprint
                or receipt.pair_id != session.pair_id
                or receipt.space_id != session.space_id
                or receipt.source_fingerprint != approval.source_fingerprint
                or receipt.target_fingerprint != session.target_fingerprint
                or receipt.source_node_id != event.origin_node_id
                or receipt.target_node_id != target_node_id
                or receipt.base_epoch != base
                or receipt.membership_epoch != base + 2
                or receipt.activation_event_id != event.event_id
                or receipt.membership_view_digest
                != event.payload.get("candidate_view_digest")
            ):
                return None
            return signed
        except Exception:
            return None

    async def _target_terminal_chain_matches_source_receipt(
        self,
        session: MeshPairingSession,
        signed_source: SignedSourceActivationReceipt,
        *,
        base: int,
    ) -> SignedTargetActivationReceipt | None:
        """Validate a terminal source receipt against retained e+1 and local e+2.

        This is deliberately the *post-terminal* proof: it uses immutable signed
        bootstrap bytes plus the exact local e+2 membership projection, but does
        not require the live term/token/pointer/commit tuple to remain at e+1.
        Once the target has released its final reservation, normal all-ACK
        ``BANK_COMMIT`` work may advance that tuple.  The caller must therefore
        restrict use to a replay of the expected source-signed terminal receipt;
        this helper is never an authority for first promotion.
        """

        try:
            if session.role != MeshPairingRole.TARGET.value:
                return None
            approval = await self._target_enrollment_approval(session)
            signed_source.verify(approval.source_public_key)
            source_receipt = signed_source.receipt
            embedded_target = SignedTargetActivationReceipt.from_dict(
                source_receipt.target_activation_receipt
            )
            embedded_target.verify(self._config.public_key)
            target_receipt = embedded_target.receipt
            authority = target_receipt.authority
            target_digest = hashlib.sha256(
                embedded_target.canonical_bytes()
            ).hexdigest()
            if (
                source_receipt.pair_id != session.pair_id
                or source_receipt.space_id != session.space_id
                or source_receipt.source_fingerprint != session.source_fingerprint
                or source_receipt.target_fingerprint != session.target_fingerprint
                or source_receipt.base_epoch != base
                or source_receipt.membership_epoch != base + 2
                or source_receipt.target_activation_receipt_digest != target_digest
                or target_receipt.membership_epoch != base + 2
                or authority.pair_id != session.pair_id
                or authority.space_id != session.space_id
                or authority.source_fingerprint != session.source_fingerprint
                or authority.target_fingerprint != session.target_fingerprint
                or source_receipt.membership_view_digest
                != target_receipt.membership_view_digest
            ):
                return None
            snapshot_models = await self._import_authority_snapshot_matches(
                session, authority, base=base
            )
            if snapshot_models is None:
                return None
            snapshot_membership, _term, _token, _pointer, _commit = snapshot_models
            expected_membership = projected_promotion_view(
                snapshot_membership, authority.local_node_id
            )
            if not await self._target_terminal_view_matches(
                session,
                authority,
                expected_membership,
                expected_epoch=base + 2,
                expected_view_digest=target_receipt.membership_view_digest,
            ):
                return None
            source_member = next(
                (
                    member
                    for member in expected_membership.members
                    if member.node_id == source_receipt.source_node_id
                ),
                None,
            )
            if (
                source_member is None
                or source_member.status != MemberStatus.ACTIVE.value
                or source_member.public_key
                != _legacy_membership_key(approval.source_public_key)
            ):
                return None
            return embedded_target
        except Exception:
            return None

    async def _restore_target_final_confirmation_from_source_receipt(
        self,
        session: MeshPairingSession,
        signed_source: SignedSourceActivationReceipt,
        *,
        base: int,
        signed_terminal: SignedTargetTerminalConfirmationReceipt | None = None,
        allow_reserved_recovery: bool = False,
    ) -> bool:
        """Rehydrate a lost terminal target proof from an exact source replay.

        This narrowly repairs a *completed* all-ACK target whose local copies of
        the source receipt and/or target terminal confirmation were lost after
        its reservation was released.  A signed source replay contains the exact
        target e+2 receipt it consumed.  Rechecking that receipt against retained
        signed bootstrap bytes and the current e+2 membership permits us to
        restore only that immutable chain, even if ordinary commits later moved
        the live bank head.  Pending tails keep their reservation and must pass
        the stricter e+1 authority check in the normal final-ACK path.

        ``allow_reserved_recovery`` is deliberately narrower still: a target
        that has detected loss of its whole terminal triplet first marks itself
        UNSAFE and re-establishes its same-pair reservation.  It may release that
        fence only when the source replays both the exact source receipt *and*
        this target's already-signed terminal confirmation.  A source receipt
        alone is also emitted before the original terminal ACK, so it is never
        enough for this post-commit recovery branch.
        """

        async with self.store.space_lock(session.space_id):
            try:
                fresh = await self.store.get_session(session.pair_id)
                if (
                    fresh is None
                    or fresh.role != MeshPairingRole.TARGET.value
                    or fresh.space_id != session.space_id
                    or fresh.base_epoch != base
                    or fresh.state != MeshPairingState.ACTIVE.value
                ):
                    return False
                reservation = await self.store.get_reservation(fresh.space_id)
                if allow_reserved_recovery:
                    health = await self._hive_store(fresh.space_id).get_node_status()
                    if (
                        reservation != fresh.pair_id
                        or signed_terminal is None
                        or health is None
                        or health.status != HiveNodeStatus.UNSAFE.value
                        or health.reason != "mesh_activation_authority_lost"
                    ):
                        return False
                elif reservation is not None:
                    return False
                embedded_target = await self._target_terminal_chain_matches_source_receipt(
                    fresh, signed_source, base=base
                )
                if embedded_target is None:
                    return False

                existing_target = await self.store.get_target_activation_receipt(
                    fresh.pair_id
                )
                if existing_target is None:
                    await self.store.put_target_activation_receipt(embedded_target)
                elif (
                    existing_target.canonical_bytes()
                    != embedded_target.canonical_bytes()
                ):
                    return False

                existing_source = await self.store.get_source_activation_receipt(
                    fresh.pair_id
                )
                if existing_source is None:
                    await self.store.put_source_activation_receipt(signed_source)
                    existing_source = await self.store.get_source_activation_receipt(
                        fresh.pair_id
                    )
                elif not self._same_source_activation_receipt(
                    existing_source, signed_source
                ):
                    return False
                if existing_source is None:
                    return False
                approval = await self._target_enrollment_approval(fresh)
                existing_source.verify(approval.source_public_key)
                target_digest = hashlib.sha256(
                    embedded_target.canonical_bytes()
                ).hexdigest()
                source_digest = hashlib.sha256(
                    existing_source.canonical_bytes()
                ).hexdigest()
                existing_terminal = await self.store.get_target_terminal_confirmation(
                    fresh.pair_id
                )
                if existing_terminal is None:
                    if signed_terminal is not None:
                        signed_terminal.verify(self._config.public_key)
                        existing_terminal = signed_terminal
                    elif allow_reserved_recovery:
                        return False
                    else:
                        terminal = TargetTerminalConfirmationReceipt(
                            pair_id=fresh.pair_id,
                            protocol_version=1,
                            space_id=fresh.space_id,
                            source_fingerprint=fresh.source_fingerprint,
                            target_fingerprint=fresh.target_fingerprint,
                            base_epoch=base,
                            membership_epoch=base + 2,
                            source_activation_receipt_digest=source_digest,
                            target_activation_receipt_digest=target_digest,
                            confirmed_at_ms=self._clock_ms(),
                        )
                        existing_terminal = SignedTargetTerminalConfirmationReceipt.sign(
                            terminal, self._config.private_key
                        )
                    await self.store.put_target_terminal_confirmation(existing_terminal)
                    existing_terminal = await self.store.get_target_terminal_confirmation(
                        fresh.pair_id
                    )
                if existing_terminal is None:
                    return False
                existing_terminal.verify(self._config.public_key)
                terminal = existing_terminal.receipt
                if (
                    terminal.pair_id != fresh.pair_id
                    or terminal.space_id != fresh.space_id
                    or terminal.source_fingerprint != fresh.source_fingerprint
                    or terminal.target_fingerprint != fresh.target_fingerprint
                    or terminal.base_epoch != base
                    or terminal.membership_epoch != base + 2
                    or terminal.source_activation_receipt_digest != source_digest
                    or terminal.target_activation_receipt_digest != target_digest
                ):
                    return False
                await self._settle_target_pairing_fence(
                    fresh,
                    target_receipt=embedded_target,
                    source_receipt=existing_source,
                    terminal_confirmation=existing_terminal,
                )
                if allow_reserved_recovery:
                    await self.store.release(fresh.space_id, fresh.pair_id)
                return await self._target_finalized_activation_matches(
                    fresh, base=base
                )
            except Exception:
                return False

    async def _source_terminal_confirmation_matches(
        self, session: MeshPairingSession
    ) -> bool:
        """Verify that a source has a durable target-signed terminal readback."""

        try:
            if session.role != MeshPairingRole.SOURCE.value:
                return False
            approval = await self._source_enrollment_approval(session)
            source_signed = await self.store.get_source_activation_receipt(
                session.pair_id
            )
            terminal_signed = await self.store.get_target_terminal_confirmation(
                session.pair_id
            )
            if source_signed is None or terminal_signed is None:
                return False
            source_signed.verify(self._config.public_key)
            terminal_signed.verify(approval.target_public_key)
            source_receipt = source_signed.receipt
            terminal = terminal_signed.receipt
            return (
                source_receipt.pair_id == session.pair_id
                and source_receipt.space_id == session.space_id
                and source_receipt.source_fingerprint == session.source_fingerprint
                and source_receipt.target_fingerprint == session.target_fingerprint
                and source_receipt.base_epoch == session.base_epoch
                and source_receipt.membership_epoch == session.base_epoch + 2
                and terminal.pair_id == session.pair_id
                and terminal.space_id == session.space_id
                and terminal.source_fingerprint == session.source_fingerprint
                and terminal.target_fingerprint == session.target_fingerprint
                and terminal.base_epoch == session.base_epoch
                and terminal.membership_epoch == session.base_epoch + 2
                and terminal.source_activation_receipt_digest
                == hashlib.sha256(source_signed.canonical_bytes()).hexdigest()
                and terminal.target_activation_receipt_digest
                == source_receipt.target_activation_receipt_digest
            )
        except Exception:
            return False

    async def _target_activation_response_payload(
        self,
        session: MeshPairingSession,
        *,
        base: int,
        source_terminal_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Build the signed response fields consumed by the source tail.

        The source receives this body inside a target-signed wire response and
        binds the digest into its own terminal receipt.  The digest is therefore
        not a mutable workflow hint: it names the exact target-signed e+2 proof
        the target will later re-read before releasing its reservation.
        """

        signed = await self.store.get_target_activation_receipt(session.pair_id)
        if signed is None:
            raise MeshPairingServiceError(
                "activation_receipt_missing", "target activation receipt is unavailable"
            )
        signed.verify(self._config.public_key)
        if (
            signed.receipt.membership_epoch != base + 2
            or signed.receipt.authority.pair_id != session.pair_id
            or signed.receipt.authority.space_id != session.space_id
        ):
            raise MeshPairingServiceError(
                "activation_receipt_mismatch", "target activation receipt is invalid"
            )
        payload: dict[str, Any] = {
            "pair_id": session.pair_id,
            "state": MeshPairingState.ACTIVE.value,
            "epoch": base + 2,
            "target_activation_receipt_digest": hashlib.sha256(
                signed.canonical_bytes()
            ).hexdigest(),
            "target_activation_receipt": signed.as_dict(),
            "source_terminal_confirmed": source_terminal_confirmed,
        }
        # A duplicate e+2 sent after the full all-ACK tail may be the only safe
        # way for a source or target that lost one local terminal copy to
        # reconstruct the exact signed bytes.  Surface that already-finalized
        # chain in the authenticated response; never infer it from session
        # state or a mutable head.  During the original tail callers pass
        # ``source_terminal_confirmed=True`` after persisting these records.
        finalized = source_terminal_confirmed or await self._target_finalized_activation_matches(
            session, base=base
        )
        if finalized:
            confirmation = await self.store.get_target_terminal_confirmation(
                session.pair_id
            )
            source_receipt = await self.store.get_source_activation_receipt(
                session.pair_id
            )
            if confirmation is None or source_receipt is None:
                raise MeshPairingServiceError(
                    "terminal_confirmation_missing",
                    "target terminal confirmation is unavailable",
                )
            confirmation.verify(self._config.public_key)
            approval = await self._target_enrollment_approval(session)
            source_receipt.verify(approval.source_public_key)
            payload["source_terminal_confirmed"] = True
            payload["source_activation_receipt"] = source_receipt.as_dict()
            payload["target_terminal_confirmation"] = confirmation.as_dict()
        return payload

    @staticmethod
    def _same_source_activation_receipt(
        left: SignedSourceActivationReceipt, right: SignedSourceActivationReceipt
    ) -> bool:
        left_data = left.receipt.as_dict()
        right_data = right.receipt.as_dict()
        left_data.pop("confirmed_at_ms")
        right_data.pop("confirmed_at_ms")
        return left_data == right_data

    async def _complete_target_source_terminal_confirmation(
        self,
        session: MeshPairingSession,
        envelope: MeshRequestEnvelope,
        event: EventEnvelope,
        *,
        base: int,
    ) -> HandlerResult | None:
        """Persist/verify the source's all-ACK receipt, then release target fence.

        An ordinary target write remains refused from its e+2 apply until this
        target receives a source-signed receipt bound to the target's own signed
        e+2 receipt.  This closes the source-crash window without treating an
        unsigned post-e+2 BANK_COMMIT head as proof of the original import.
        """

        async with self.store.space_lock(session.space_id):
            fresh = await self.store.get_session(session.pair_id)
            if (
                fresh is None
                or fresh.role != MeshPairingRole.TARGET.value
                or fresh.space_id != session.space_id
                or fresh.base_epoch != base
                or fresh.state != MeshPairingState.ACTIVE.value
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            return await self._complete_target_source_terminal_confirmation_locked(
                fresh, envelope, event, base=base
            )

    async def _complete_target_source_terminal_confirmation_locked(
        self,
        session: MeshPairingSession,
        envelope: MeshRequestEnvelope,
        event: EventEnvelope,
        *,
        base: int,
    ) -> HandlerResult | None:
        """Lock-held implementation of source terminal confirmation."""

        raw = event.payload.get("source_activation_receipt")
        if raw is None:
            return None
        try:
            signed = SignedSourceActivationReceipt.from_dict(raw)
            approval = await self._target_enrollment_approval(session)
            signed.verify(approval.source_public_key)
            receipt = signed.receipt
            embedded_target_receipt = SignedTargetActivationReceipt.from_dict(
                receipt.target_activation_receipt
            )
            embedded_target_receipt.verify(self._config.public_key)
            target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
            source_node_id = event.origin_node_id
            if (
                envelope.source_public_key != approval.source_public_key
                or envelope.source_fingerprint != approval.source_fingerprint
                or receipt.pair_id != session.pair_id
                or receipt.space_id != session.space_id
                or receipt.source_fingerprint != approval.source_fingerprint
                or receipt.target_fingerprint != session.target_fingerprint
                or receipt.source_node_id != source_node_id
                or receipt.target_node_id != target_node_id
                or receipt.base_epoch != base
                or receipt.membership_epoch != base + 2
                or receipt.activation_event_id != event.event_id
                or receipt.membership_view_digest
                != event.payload.get("candidate_view_digest")
                or receipt.target_activation_receipt_digest
                != hashlib.sha256(
                    embedded_target_receipt.canonical_bytes()
                ).hexdigest()
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            existing = await self.store.get_source_activation_receipt(session.pair_id)
            reservation = await self.store.get_reservation(session.space_id)
            if existing is not None:
                existing.verify(approval.source_public_key)
                if not self._same_source_activation_receipt(existing, signed):
                    return _refuse(MeshResponseCode.LOCAL_UNSAFE)
                # This exact source receipt was already verified before the
                # reservation was released.  A later ordinary BANK_COMMIT may
                # legitimately advance the head, so a duplicate signed delivery
                # is an acknowledgement only and never replays a transition.
                if reservation is None:
                    return _ok(
                        await self._target_activation_response_payload(
                            session,
                            base=base,
                            source_terminal_confirmed=True,
                        )
                    )
            if reservation != session.pair_id:
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            # A source crash can persist its terminal record before this target
            # has durably retained its own e+2 receipt.  The embedded target
            # signature restores only the exact canonical receipt whose digest
            # the source signed; it never invents a timestamp-different proof.
            target_receipt = await self.store.get_target_activation_receipt(
                session.pair_id
            )
            if target_receipt is None:
                await self.store.put_target_activation_receipt(embedded_target_receipt)
                target_receipt = await self.store.get_target_activation_receipt(
                    session.pair_id
                )
            if (
                target_receipt is None
                or target_receipt.canonical_bytes()
                != embedded_target_receipt.canonical_bytes()
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            # Until the terminal source receipt has been durably accepted, the
            # target reservation prohibits ordinary writes.  Therefore exact
            # e+1 import equality remains the correct corruption boundary here.
            if not await self._target_activation_receipt_matches(session, base=base):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            target_receipt.verify(self._config.public_key)
            if receipt.target_activation_receipt_digest != hashlib.sha256(
                target_receipt.canonical_bytes()
            ).hexdigest():
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            await self.store.put_source_activation_receipt(signed)
            stored = await self.store.get_source_activation_receipt(session.pair_id)
            if stored is None:
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            stored.verify(approval.source_public_key)
            if not self._same_source_activation_receipt(stored, signed):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            terminal = TargetTerminalConfirmationReceipt(
                pair_id=session.pair_id,
                protocol_version=1,
                space_id=session.space_id,
                source_fingerprint=session.source_fingerprint,
                target_fingerprint=session.target_fingerprint,
                base_epoch=base,
                membership_epoch=base + 2,
                source_activation_receipt_digest=hashlib.sha256(
                    stored.canonical_bytes()
                ).hexdigest(),
                target_activation_receipt_digest=receipt.target_activation_receipt_digest,
                confirmed_at_ms=self._clock_ms(),
            )
            signed_terminal = SignedTargetTerminalConfirmationReceipt.sign(
                terminal, self._config.private_key
            )
            await self.store.put_target_terminal_confirmation(signed_terminal)
            stored_terminal = await self.store.get_target_terminal_confirmation(
                session.pair_id
            )
            if stored_terminal is None:
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            stored_terminal.verify(self._config.public_key)
            if (
                stored_terminal.receipt.source_activation_receipt_digest
                != terminal.source_activation_receipt_digest
                or stored_terminal.receipt.target_activation_receipt_digest
                != terminal.target_activation_receipt_digest
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            await self._settle_target_pairing_fence(
                session,
                target_receipt=target_receipt,
                source_receipt=stored,
                terminal_confirmation=stored_terminal,
            )
            await self.store.release(session.space_id, session.pair_id)
            return _ok(
                await self._target_activation_response_payload(
                    session,
                    base=base,
                    source_terminal_confirmed=True,
                )
            )
        except Exception:
            return _refuse(MeshResponseCode.LOCAL_UNSAFE)

    async def _fence_target_activation_authority_loss(
        self, session: MeshPairingSession, *, base: int
    ) -> bool:
        """Take a target out of service when both terminal authorities are lost.

        ``ACTIVE`` is operational bookkeeping, never a replacement for the
        retained e+1 import proof or target-signed e+2 receipt.  This narrow
        fence deliberately preserves the terminal session state (there is no
        generic ACTIVE -> BLOCKED transition): it marks the space UNSAFE and
        re-establishes its same-pair reservation under the target space lock.
        A later source-authenticated e+2 event may reconstruct a new terminal
        receipt; without that event the target stays fenced rather than silently
        replaying or tearing down a live e+2 space.
        """

        async with self.store.space_lock(session.space_id):
            fresh = await self.store.get_session(session.pair_id)
            if (
                fresh is None
                or fresh.role != MeshPairingRole.TARGET.value
                or fresh.space_id != session.space_id
                or fresh.base_epoch != base
            ):
                return False
            if (
                await self._import_validation_matches(fresh, base=base)
                or await self._target_activation_receipt_matches(fresh, base=base)
                or await self._target_finalized_activation_matches(fresh, base=base)
            ):
                return False
            try:
                await self._arm_target_pairing_fence(
                    fresh, rearm_recovery=True
                )
                await self._hive_store(fresh.space_id).set_node_status(
                    NodeHealth(
                        status=HiveNodeStatus.UNSAFE,
                        reason="mesh_activation_authority_lost",
                    )
                )
                await self.store.reserve(
                    fresh.space_id, fresh.pair_id, now_ms=self._clock_ms()
                )
            except Exception:
                return False
            return True

    async def _fence_active_target_terminal_chain_loss(
        self, session: MeshPairingSession, *, base: int
    ) -> bool:
        """Re-establish the target fence before repairing a lost final chain.

        This handles only a current #417 target which is already ACTIVE at its
        exact e+2 but has lost one or more terminal records after its reservation
        was released.  The immutable acceptance intent distinguishes this from
        legacy receipt-less terminal history.  It deliberately does *not*
        promote or accept a mutable head: it merely makes the target UNSAFE and
        re-reserves it until the source replays the complete signed tail.
        """

        async with self.store.space_lock(session.space_id):
            try:
                fresh = await self.store.get_session(session.pair_id)
                if (
                    fresh is None
                    or fresh.role != MeshPairingRole.TARGET.value
                    or fresh.state != MeshPairingState.ACTIVE.value
                    or fresh.space_id != session.space_id
                    or fresh.base_epoch != base
                    or await self.store.get_reservation(fresh.space_id) is not None
                ):
                    return False
                intent = await self.store.get_target_acceptance_intent(fresh.pair_id)
                if (
                    intent is None
                    or intent["space_id"] != fresh.space_id
                    or intent["source_fingerprint"] != fresh.source_fingerprint
                    or intent["target_fingerprint"] != fresh.target_fingerprint
                    or await self._target_finalized_activation_matches(fresh, base=base)
                ):
                    return False
                store = self._hive_store(fresh.space_id)
                node, membership = await asyncio.gather(
                    store.get_node_identity(), store.get_membership()
                )
                if (
                    node is None
                    or membership is None
                    or membership.epoch != base + 2
                    or node.node_id
                    != _node_id_from_fingerprint(fresh.target_fingerprint)
                    or not any(
                        member.node_id == node.node_id
                        and member.status == MemberStatus.ACTIVE.value
                        for member in membership.members
                    )
                ):
                    return False
                await self._arm_target_pairing_fence(
                    fresh, rearm_recovery=True
                )
                await store.set_node_status(
                    NodeHealth(
                        status=HiveNodeStatus.UNSAFE,
                        reason="mesh_activation_authority_lost",
                    )
                )
                await self.store.reserve(
                    fresh.space_id, fresh.pair_id, now_ms=self._clock_ms()
                )
                return True
            except Exception:
                return False

    async def _restore_target_terminal_receipt_from_event(
        self,
        session: MeshPairingSession,
        envelope: MeshRequestEnvelope,
        event: EventEnvelope,
        *,
        base: int,
    ) -> bool:
        """Recreate a lost terminal receipt from the expected source's e+2 event.

        This path is intentionally unavailable to a local admin retry or a
        mutable ``ACTIVE`` workflow record.  It needs the retained source-signed
        e+1 snapshot, exact local e+2 projection, and an authenticated e+2 event
        from the source member named by that snapshot.  The target remains
        UNSAFE/reserved until the new target-signed receipt has read back.
        """

        async with self.store.space_lock(session.space_id):
            fresh = await self.store.get_session(session.pair_id)
            if (
                fresh is None
                or fresh.role != MeshPairingRole.TARGET.value
                or fresh.space_id != session.space_id
                or fresh.base_epoch != base
                or fresh.state
                not in (
                    MeshPairingState.AWAITING_ACKS.value,
                    MeshPairingState.BLOCKED_RECOVERY.value,
                    MeshPairingState.ACTIVE.value,
                )
            ):
                return False
            if await self._target_activation_receipt_matches(fresh, base=base):
                return True
            try:
                (
                    authority,
                    snapshot_membership,
                    _snapshot_term,
                    _snapshot_token,
                    _snapshot_pointer,
                    _snapshot_commit,
                ) = await self._retained_import_authority(fresh, base=base)
                if not await self._import_authority_matches(
                    fresh, authority, base=base
                ):
                    return False
                expected_membership = projected_promotion_view(
                    snapshot_membership, authority.local_node_id
                )
                expected_digest = candidate_view_digest(expected_membership)
                payload = event.payload if isinstance(event.payload, dict) else {}
                if (
                    event.type != EventType.MEMBERSHIP_UPDATED.value
                    or event.membership_epoch != base + 2
                    or event.origin_node_id == ""
                    or payload.get("pair_id") != fresh.pair_id
                    or payload.get("node_id") != authority.local_node_id
                    or payload.get("status") != MemberStatus.ACTIVE.value
                    or payload.get("epoch") != base + 2
                    or payload.get("candidate_view_digest") != expected_digest
                ):
                    return False
                store = self._hive_store(fresh.space_id)
                membership = await store.get_membership()
                if (
                    membership is None
                    or not _source_event_is_eligible(
                        membership,
                        envelope.source_public_key,
                        event.origin_node_id,
                    )
                    or not await self._target_terminal_view_matches(
                        fresh,
                        authority,
                        expected_membership,
                        expected_epoch=base + 2,
                        expected_view_digest=expected_digest,
                    )
                ):
                    return False
                await self._arm_target_pairing_fence(
                    fresh, rearm_recovery=True
                )
                await store.set_node_status(
                    NodeHealth(
                        status=HiveNodeStatus.UNSAFE,
                        reason="mesh_activation_authority_lost",
                    )
                )
                await self.store.reserve(
                    fresh.space_id, fresh.pair_id, now_ms=self._clock_ms()
                )
                # A malformed or semantically rejected old receipt must not
                # prevent the exact authenticated e+2 proof from replacing it.
                await self.store.clear_target_activation_receipt_for_recovery(
                    fresh.pair_id
                )
                receipt = TargetActivationReceipt(
                    authority=authority,
                    membership_epoch=base + 2,
                    membership_view_digest=expected_digest,
                    activated_at_ms=self._clock_ms(),
                )
                signed = SignedTargetActivationReceipt.sign(
                    receipt, self._config.private_key
                )
                await self.store.put_target_activation_receipt(signed)
                stored = await self.store.get_target_activation_receipt(fresh.pair_id)
                if stored is None:
                    return False
                stored.verify(self._config.public_key)
                return stored.receipt == receipt
            except Exception:
                return False

    async def _mark_target_import_validation_failure(
        self, session: MeshPairingSession
    ) -> None:
        """Fail a target closed and make a pre-terminal proof loss resyncable.

        A missing, corrupt, or mismatched import authority is not a harmless
        retry condition.  Before the terminal receipt it must become durable
        ``resync`` work; otherwise source e+2 / target UNSAFE can deadlock with
        neither normal enrollment nor abandonment able to repair the target.
        """

        # Once the terminal target receipt is durable, the marker is retained
        # audit evidence rather than a gate for a new promotion.  Conversely an
        # ACTIVE record with neither marker nor receipt is not authority: fence
        # it until the expected source re-delivers an exact e+2 event and a new
        # signed terminal receipt can be reconstructed.
        try:
            fresh = await self.store.get_session(session.pair_id)
            if fresh is not None and fresh.state == MeshPairingState.ACTIVE.value:
                await self._fence_target_activation_authority_loss(
                    fresh, base=fresh.base_epoch
                )
                return
        except Exception:
            fresh = None
        store = self._hive_store(session.space_id)
        try:
            await store.set_node_status(
                NodeHealth(
                    status=HiveNodeStatus.UNSAFE,
                    reason="mesh_import_validation_failed",
                )
            )
        except Exception:
            # The caller will still return LOCAL_UNSAFE.  Never compensate a
            # failed unsafe marker with a promotion or a healthy transition.
            pass
        try:
            fresh = await self.store.get_session(session.pair_id)
            if (
                fresh is not None
                and fresh.role == MeshPairingRole.TARGET.value
                and fresh.state
                in (
                    MeshPairingState.TRANSFERRING.value,
                    MeshPairingState.AWAITING_ACKS.value,
                    MeshPairingState.BLOCKED_RECOVERY.value,
                )
            ):
                await self._block_recovery(
                    fresh,
                    phase="import_validation_failed",
                    next_action="resync",
                    manifest_digest=fresh.bootstrap_manifest_digest,
                )
        except Exception:
            # Returning LOCAL_UNSAFE remains the fail-closed response if durable
            # diagnostics cannot be written; do not let this operational tail
            # mask the authority mismatch.
            pass

    async def _block_target_import_failure(
        self, session: MeshPairingSession, *, manifest_digest: str
    ) -> MeshPairingSession:
        """Record an import failure without overwriting a raced ACTIVE receipt.

        The enrollment/resync caller holds the pair lock while an inbound e+2
        handler intentionally does not.  This space-tail lock is therefore the
        local linearization point for a late import exception and terminal
        self-activation.
        """

        async with self.store.space_lock(session.space_id):
            fresh = await self.store.get_session(session.pair_id)
            if fresh is None or fresh.role != MeshPairingRole.TARGET.value:
                raise MeshPairingServiceError(
                    "import_failed", "target pairing state disappeared during import"
                )
            if fresh.state == MeshPairingState.ACTIVE.value:
                return fresh
            if fresh.state not in (
                MeshPairingState.TRANSFERRING.value,
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
            ):
                raise MeshPairingServiceError(
                    "import_failed", "target pairing changed during import"
                )
            return await self._block_recovery(
                fresh,
                phase="bootstrap_import_failed",
                next_action="resync",
                manifest_digest=manifest_digest,
            )

    # ==================================================================
    # Pairing-activation fence (registered into the membership layer)
    # ==================================================================

    def _source_activation_migration_authority(
        self, session: MeshPairingSession
    ) -> SignedSourceActivationMigrationAuthority:
        """Create the signed, bounded owner record for a new source tail."""

        if (
            session.role != MeshPairingRole.SOURCE.value
            or not session.target_fingerprint
        ):
            raise MeshPairingServiceError(
                "invalid_source_activation_migration",
                "source activation migration requires an enrolled target",
            )
        authority = SourceActivationMigrationAuthority(
            pair_id=session.pair_id,
            protocol_version=1,
            space_id=session.space_id,
            source_fingerprint=session.source_fingerprint,
            target_fingerprint=session.target_fingerprint,
            base_epoch=session.base_epoch,
            requires_terminal_confirmation=True,
            issued_at_ms=self._clock_ms(),
        )
        return SignedSourceActivationMigrationAuthority.sign(
            authority, self._config.private_key
        )

    def _source_activation_migration_matches_session(
        self,
        signed: SignedSourceActivationMigrationAuthority | None,
        session: MeshPairingSession,
    ) -> bool:
        """Verify the source-signed index cannot be retargeted by storage."""

        try:
            if signed is None:
                return False
            signed.verify(self._config.public_key)
            authority = signed.authority
            return (
                authority.requires_terminal_confirmation
                and authority.pair_id == session.pair_id
                and authority.space_id == session.space_id
                and authority.source_fingerprint == session.source_fingerprint
                and authority.target_fingerprint == session.target_fingerprint
                and authority.base_epoch == session.base_epoch
            )
        except Exception:
            return False

    async def _is_new_source_terminal_tail(
        self, session: MeshPairingSession
    ) -> bool:
        """Return whether a source session carries durable #417 provenance."""

        if (
            session.role != MeshPairingRole.SOURCE.value
            or session.state != MeshPairingState.ACTIVE.value
        ):
            return False
        evidence, marker = await asyncio.gather(
            self.store.get_source_bootstrap_evidence(session.pair_id),
            self.store.get_source_activation_marker(session.space_id),
        )
        if evidence is not None:
            evidence.verify(self._config.public_key)
        if marker is not None:
            marker.verify(self._config.public_key)
        return evidence is not None or (
            marker is not None and marker.evidence.pair_id == session.pair_id
        )

    async def _source_activation_marker_matches_current_tail(
        self,
        marker: SignedSourceBootstrapEvidence,
        session: MeshPairingSession,
        membership: MembershipView | None,
    ) -> bool:
        """Bind a per-space marker to the source's *current* activation tail.

        The marker is a signed, per-space provenance index rather than an
        append-only history entry.  A raw storage rollback can otherwise replay
        a valid marker from a completed, evicted pairing and make the ordinary
        write guard release it while a newer e+2 tail is still unconfirmed.
        Current membership is monotonic protocol authority, so the marker must
        name its present PENDING e+1 candidate or ACTIVE e+2 member at the
        matching epoch before it can be used for either release or an owner
        bypass.
        """

        try:
            marker.verify(self._config.public_key)
            evidence = marker.evidence
            if (
                membership is None
                or session.role != MeshPairingRole.SOURCE.value
                or session.pair_id != evidence.pair_id
                or session.space_id != evidence.space_id
                or session.source_fingerprint != evidence.source_fingerprint
                or session.target_fingerprint != evidence.target_fingerprint
            ):
                return False
            approval = await self._source_enrollment_approval(session)
            if (
                approval.target_fingerprint != evidence.target_fingerprint
                or approval.source_fingerprint != evidence.source_fingerprint
            ):
                return False
            target_node_id = _node_id_from_fingerprint(evidence.target_fingerprint)
            target_member = next(
                (
                    member
                    for member in membership.members
                    if member.node_id == target_node_id
                ),
                None,
            )
            expected_target_key = _legacy_membership_key(approval.target_public_key)
            base = evidence.membership_epoch
            if session.state in (
                MeshPairingState.APPROVED.value,
                MeshPairingState.TRANSFERRING.value,
            ):
                # A hard crash can land immediately after the per-space marker
                # write but before ``_export_and_store_bootstrap`` persists the
                # already-created TRANSFERRING session.  Transition 1 is then
                # durable (the candidate is PENDING at e+1) while the source
                # session still says APPROVED.  It is current authority only
                # for that exact e+1/PENDING tail; the owning eviction gets the
                # narrow ``ignore_pair_id`` bypass below, while every ordinary
                # membership mutation remains fenced.
                return bool(
                    membership.epoch == base
                    and target_member is not None
                    and target_member.status == MemberStatus.PENDING.value
                    and target_member.public_key == expected_target_key
                    and target_member.incarnation == session.pair_id
                )
            if session.state in (
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
            ):
                return bool(
                    target_member is not None
                    and target_member.public_key == expected_target_key
                    and (
                        (
                            membership.epoch == base
                            and target_member.status == MemberStatus.PENDING.value
                            and target_member.incarnation == session.pair_id
                        )
                        or (
                            membership.epoch == base + 1
                            and target_member.status == MemberStatus.ACTIVE.value
                            and target_member.incarnation == session.pair_id
                        )
                    )
                )
            if session.state == MeshPairingState.ACTIVE.value:
                return bool(
                    membership.epoch == base + 1
                    and target_member is not None
                    and target_member.status == MemberStatus.ACTIVE.value
                    and target_member.public_key == expected_target_key
                    and target_member.incarnation == session.pair_id
                )
            if session.state == MeshPairingState.CANCELLED.value:
                # The owning eviction releases this marker immediately after
                # its epoch advance.  A crash in that short tail may be safely
                # completed only at the directly adjacent post-eviction epoch.
                # ``remove_pending_candidate`` / ``evict_member`` retain an
                # EVICTED audit member, so both the absent and retained forms
                # are recognized only when the exact pair incarnation and key
                # still match.  An older cancelled marker is never current
                # authority.
                if membership.epoch not in {base + 1, base + 2}:
                    return False
                if target_member is None:
                    return True
                return bool(
                    target_member.status == MemberStatus.EVICTED.value
                    and target_member.public_key == expected_target_key
                    and target_member.incarnation == session.pair_id
                )
            return False
        except Exception:
            return False

    async def _assert_new_source_terminal_tails_confirmed(
        self, space_id: str, membership: MembershipView | None
    ) -> None:
        """Fail closed if an e+2 source tail lost its mutable fence record.

        For a #417 source a signed activation-marker copy survives independently
        of the primary export evidence and the mutable activation fence.  The
        direct per-space marker is checked by :meth:`assert_no_pairing_activation`
        before this compatibility helper; the membership-bound check below keeps
        the established no-history-scan behavior for older records.
        """

        if membership is None:
            return
        for member in active_members(membership):
            if not member.incarnation:
                continue
            session = await self.store.get_session(member.incarnation)
            if session is None:
                continue
            if (
                session.role == MeshPairingRole.SOURCE.value
                and session.space_id == space_id
                and session.state == MeshPairingState.ACTIVE.value
                and await self._is_new_source_terminal_tail(session)
                and not await self._source_terminal_confirmation_matches(session)
            ):
                raise PairingActivationError(space_id)

    @staticmethod
    def _target_requested_scopes_digest(session: MeshPairingSession) -> str:
        return hashlib.sha256(
            canonical_dumps(list(session.granted_scopes))
        ).hexdigest()

    async def _target_acceptance_intent_matches_session(
        self, session: MeshPairingSession
    ) -> bool | None:
        """Bind a target session to its direct #417 acceptance provenance.

        This O(1) record is not a terminal receipt.  It is only the durable
        discriminator used to prevent a complete loss of the three target
        fence records from downgrading a known #417 pre-mutation target to the
        legacy compatibility branch during its own cancellation cleanup.
        """

        intent = await self.store.get_target_acceptance_intent(session.pair_id)
        if intent is None:
            return None
        if (
            session.role == MeshPairingRole.TARGET.value
            and intent["pair_id"] == session.pair_id
            and intent["space_id"] == session.space_id
            and intent["source_fingerprint"] == session.source_fingerprint
            and intent["target_fingerprint"] == session.target_fingerprint
            and intent["invitation_digest"] == session.invitation_digest
            and tuple(intent["requested_scopes"]) == session.granted_scopes
        ):
            return True
        raise MeshPairingServiceError(
            "target_fence_invalid",
            "target acceptance intent does not bind the cancellation tail",
        )

    async def _target_pre_mutation_release_matches(
        self, session: MeshPairingSession
    ) -> bool | None:
        """Return the durable proof for a retrying target cancellation.

        ``MeshPairingSession`` is an operational record and its state alone is
        never authority to manufacture a released target fence.  A normal
        pre-mutation cancellation writes the signed released direct tail while
        the session is still demonstrably pre-mutation, then persists
        ``CANCELLED`` and releases the raw reservation.  Therefore a later
        ``CANCELLED`` retry may only finish the raw-delete tail when that exact
        signed released authority already exists.  ``None`` is the deliberately
        narrow pre-#417 compatibility shape: no intent, anchor, floor, fence,
        or current-tail exists at all.
        """

        intent_matches = await self._target_acceptance_intent_matches_session(session)
        anchor, floor, current_tail, fence = await asyncio.gather(
            self.store.get_target_pairing_admission_anchor(session.space_id),
            self.store.get_target_pairing_protocol_floor(session.space_id),
            self.store.get_target_pairing_current_tail(session.space_id),
            self.store.get_target_pairing_fence(session.space_id),
        )
        if floor is None and current_tail is None and fence is None:
            return None if intent_matches is None and anchor is None else False
        if floor is None or current_tail is None or fence is None:
            return False
        try:
            for signed in (floor, current_tail, fence):
                signed.verify(self._config.public_key)
            if anchor is not None:
                anchor.verify(self._config.public_key)
                if (
                    anchor.anchor.space_id != session.space_id
                    or anchor.anchor.target_public_key != self._config.public_key
                    or anchor.anchor.target_fingerprint != self._config.fingerprint
                ):
                    return False
        except Exception:
            return False
        if current_tail.canonical_bytes() != fence.canonical_bytes():
            return False
        expected = self._target_pairing_fence_authority(session, phase="released")
        expected_data = expected.as_dict()
        observed_data = fence.authority.as_dict()
        expected_data.pop("issued_at_ms")
        observed_data.pop("issued_at_ms")
        return observed_data == expected_data

    def _target_pairing_fence_authority(
        self,
        session: MeshPairingSession,
        *,
        phase: str,
        target_receipt: SignedTargetActivationReceipt | None = None,
        source_receipt: SignedSourceActivationReceipt | None = None,
        terminal_confirmation: SignedTargetTerminalConfirmationReceipt | None = None,
    ) -> TargetPairingFenceAuthority:
        """Build the signed direct ordinary-write authority for one target tail."""

        if (
            session.role != MeshPairingRole.TARGET.value
            or session.target_fingerprint != self._config.fingerprint
            or session.target_public_key != self._config.public_key
        ):
            raise MeshPairingServiceError(
                "target_fence_invalid", "target pairing fence identity is invalid"
            )
        return TargetPairingFenceAuthority(
            pair_id=session.pair_id,
            protocol_version=1,
            phase=phase,
            space_id=session.space_id,
            source_public_key=session.source_public_key,
            source_fingerprint=session.source_fingerprint,
            target_public_key=session.target_public_key,
            target_fingerprint=session.target_fingerprint,
            invitation_digest=session.invitation_digest,
            requested_scopes_digest=self._target_requested_scopes_digest(session),
            base_epoch=session.base_epoch,
            target_activation_receipt=(
                None if target_receipt is None else target_receipt.as_dict()
            ),
            source_activation_receipt=(
                None if source_receipt is None else source_receipt.as_dict()
            ),
            target_terminal_confirmation=(
                None
                if terminal_confirmation is None
                else terminal_confirmation.as_dict()
            ),
            issued_at_ms=self._clock_ms(),
        )

    def _target_pairing_admission_anchor(
        self, session: MeshPairingSession
    ) -> TargetPairingAdmissionAnchor:
        """Build the permanent direct #417 discriminator for this target space."""

        if (
            session.role != MeshPairingRole.TARGET.value
            or session.target_fingerprint != self._config.fingerprint
            or session.target_public_key != self._config.public_key
        ):
            raise MeshPairingServiceError(
                "target_fence_invalid", "target pairing anchor identity is invalid"
            )
        return TargetPairingAdmissionAnchor(
            protocol_version=1,
            space_id=session.space_id,
            target_public_key=session.target_public_key,
            target_fingerprint=session.target_fingerprint,
            issued_at_ms=self._clock_ms(),
        )

    async def migrate_target_pairing_admission_anchors(self) -> None:
        """One-shot startup backfill for retained pre-index #417 intents.

        Early #417 previews persisted immutable target acceptance intents but
        not the direct ordinary-write anchor/fence records.  The ordinary guard
        must stay O(1), so startup materializes only the permanent per-space
        discriminator from that bounded intent namespace and records success
        only after every item read back.  Any I/O, shape, ownership, or overflow
        ambiguity aborts startup rather than treating a known #417 target as
        legacy.
        """

        if await self.store.target_pairing_admission_anchor_migration_complete():
            return
        intents = await self.store.list_target_acceptance_intents_for_migration(
            max_intents=MAX_TARGET_ACCEPTANCE_INTENTS_MIGRATION
        )
        for _pair_id, intent in intents:
            if intent["target_fingerprint"] != self._config.fingerprint:
                raise MeshPairingServiceError(
                    "target_anchor_migration_conflict",
                    "retained target acceptance intent belongs to another Mesh identity",
                )
            await self.store.put_target_pairing_admission_anchor(
                SignedTargetPairingAdmissionAnchor.sign(
                    TargetPairingAdmissionAnchor(
                        protocol_version=1,
                        space_id=intent["space_id"],
                        target_public_key=self._config.public_key,
                        target_fingerprint=self._config.fingerprint,
                        issued_at_ms=self._clock_ms(),
                    ),
                    self._config.private_key,
                )
            )
        await self.store.mark_target_pairing_admission_anchor_migration_complete()

    async def _arm_target_pairing_fence(
        self,
        session: MeshPairingSession,
        *,
        replace_settled: bool = False,
        rearm_recovery: bool = False,
    ) -> None:
        """Persist a held per-space target fence before raw reservation/session I/O."""

        authority = self._target_pairing_fence_authority(session, phase="held")
        signed = SignedTargetPairingFenceAuthority.sign(
            authority, self._config.private_key
        )
        await self.store.put_target_pairing_protocol_floor(signed)
        await self._put_target_pairing_fence_and_current_tail(
            signed,
            replace_settled=replace_settled,
            rearm_recovery=rearm_recovery,
        )
        # The permanent admission anchor is last within the direct-fence
        # prefix, but still before every caller's raw reservation.  Thus an
        # anchor-only crash cannot occur; if the anchor exists, the floor/fence
        # evidence needed by explicit recovery was already durably written.
        await self.store.put_target_pairing_admission_anchor(
            SignedTargetPairingAdmissionAnchor.sign(
                self._target_pairing_admission_anchor(session),
                self._config.private_key,
            )
        )

    async def _put_target_pairing_fence_and_current_tail(
        self,
        signed: SignedTargetPairingFenceAuthority,
        *,
        replace_settled: bool = False,
        rearm_recovery: bool = False,
    ) -> SignedTargetPairingFenceAuthority:
        """Write a fence, then index the exact read-back signed bytes.

        The current-tail index is deliberately an independent direct record,
        but it must name the *actual* fence bytes.  The store may retain an
        earlier timestamp-only idempotent authority, so indexing the locally
        constructed candidate would let two semantically equal records diverge
        and would make the ordinary-write guard either spuriously open or
        permanently reject a valid crash retry.
        """

        await self.store.put_target_pairing_fence(
            signed,
            replace_settled=replace_settled,
            rearm_recovery=rearm_recovery,
        )
        stored = await self.store.get_target_pairing_fence(signed.authority.space_id)
        if stored is None:
            raise MeshPairingServiceError(
                "target_fence_missing", "target pairing fence read-back is missing"
            )
        try:
            stored.verify(self._config.public_key)
        except Exception as exc:
            raise MeshPairingServiceError(
                "target_fence_invalid", "target pairing fence read-back is unsigned"
            ) from exc
        expected_data = signed.authority.as_dict()
        observed_data = stored.authority.as_dict()
        expected_data.pop("issued_at_ms")
        observed_data.pop("issued_at_ms")
        if observed_data != expected_data:
            raise MeshPairingServiceError(
                "target_fence_invalid", "target pairing fence read-back changed owner"
            )
        await self.store.put_target_pairing_current_tail(
            stored,
            replace_settled=replace_settled,
            rearm_recovery=rearm_recovery,
            reconcile_fence_bytes=True,
        )
        indexed = await self.store.get_target_pairing_current_tail(
            signed.authority.space_id
        )
        if indexed is None or indexed.canonical_bytes() != stored.canonical_bytes():
            raise MeshPairingServiceError(
                "target_fence_invalid", "target current-tail index read-back diverged"
            )
        return stored

    async def _settle_target_pairing_fence(
        self,
        session: MeshPairingSession,
        *,
        target_receipt: SignedTargetActivationReceipt,
        source_receipt: SignedSourceActivationReceipt,
        terminal_confirmation: SignedTargetTerminalConfirmationReceipt,
    ) -> None:
        """Persist full signed all-ACK evidence before raw reservation release."""

        authority = self._target_pairing_fence_authority(
            session,
            phase="terminal_confirmed",
            target_receipt=target_receipt,
            source_receipt=source_receipt,
            terminal_confirmation=terminal_confirmation,
        )
        signed = SignedTargetPairingFenceAuthority.sign(
            authority, self._config.private_key
        )
        # The floor is a durable protocol discriminator.  Rebuild it from the
        # exact current signed tail before allowing the raw reservation to be
        # released; otherwise a one-key loss would leave a terminal or
        # cancellation tail permanently write-fenced as a fence-only shape.
        await self.store.put_target_pairing_protocol_floor(signed)
        await self._put_target_pairing_fence_and_current_tail(signed)
        await self.store.put_target_pairing_admission_anchor(
            SignedTargetPairingAdmissionAnchor.sign(
                self._target_pairing_admission_anchor(session),
                self._config.private_key,
            )
        )

    async def _release_target_pairing_fence(self, session: MeshPairingSession) -> None:
        """Mark a proven pre-mutation target exit before releasing raw reserve."""

        floor, current_tail, existing = await asyncio.gather(
            self.store.get_target_pairing_protocol_floor(session.space_id),
            self.store.get_target_pairing_current_tail(session.space_id),
            self.store.get_target_pairing_fence(session.space_id),
        )
        if floor is None and current_tail is None and existing is None:
            # Do not retrofit target-fence provenance onto a true legacy
            # terminal history.  A retained #417 acceptance intent is the
            # narrow exception: all three mutable direct records may have been
            # independently lost, but treating that known tail as legacy would
            # let this very cancellation release the raw reservation and reopen
            # ordinary writes. Recreate a held owner first, then release it.
            if await self._target_acceptance_intent_matches_session(session) is None:
                return
            held = self._target_pairing_fence_authority(session, phase="held")
            signed_held = SignedTargetPairingFenceAuthority.sign(
                held, self._config.private_key
            )
            await self.store.put_target_pairing_protocol_floor(signed_held)
            await self._put_target_pairing_fence_and_current_tail(signed_held)
        authority = self._target_pairing_fence_authority(session, phase="released")
        signed = SignedTargetPairingFenceAuthority.sign(
            authority, self._config.private_key
        )
        await self.store.put_target_pairing_protocol_floor(signed)
        await self._put_target_pairing_fence_and_current_tail(signed)
        await self.store.put_target_pairing_admission_anchor(
            SignedTargetPairingAdmissionAnchor.sign(
                self._target_pairing_admission_anchor(session),
                self._config.private_key,
            )
        )

    async def _release_target_pairing_fence_for_orphan(
        self, *, space_id: str, pair_id: str
    ) -> None:
        """Settle an exact no-session acceptance prefix after blank-target proof."""

        floor, current_tail, signed = await asyncio.gather(
            self.store.get_target_pairing_protocol_floor(space_id),
            self.store.get_target_pairing_current_tail(space_id),
            self.store.get_target_pairing_fence(space_id),
        )

        def _local_prefix_owner(
            candidate: SignedTargetPairingFenceAuthority | None,
        ) -> TargetPairingFenceAuthority | None:
            if candidate is None:
                return None
            try:
                candidate.verify(self._config.public_key)
            except Exception as exc:
                raise MeshPairingServiceError(
                    "not_orphaned", "target pairing fence signature is invalid"
                ) from exc
            authority = candidate.authority
            if (
                authority.space_id == space_id
                and authority.pair_id == pair_id
                and authority.phase in ("held", "released")
                and authority.target_fingerprint == self._config.fingerprint
                and authority.target_public_key == self._config.public_key
            ):
                return authority
            return None

        def _same_local_prefix(
            left: TargetPairingFenceAuthority,
            right: TargetPairingFenceAuthority,
        ) -> bool:
            """Compare an orphan prefix without its local lifecycle phase/time."""

            left_data = left.as_dict()
            right_data = right.as_dict()
            for data in (left_data, right_data):
                data.pop("phase")
                data.pop("issued_at_ms")
            return left_data == right_data

        # A crash after the permanent #417 floor but before the operational
        # fence is a local-only prefix: no session has been persisted yet, so
        # no claim can have been sent.  The signed floor is sufficient to make
        # that exact blank-space operator recovery auditable.  A mismatched
        # fence is deliberately not trusted as a substitute for this owner.
        current_owner = _local_prefix_owner(current_tail)
        fence_owner = _local_prefix_owner(signed)
        floor_owner = _local_prefix_owner(floor)
        authority = current_owner or fence_owner or floor_owner
        if authority is None:
            raise MeshPairingServiceError(
                "not_orphaned", "target pairing fence does not match orphaned reservation"
            )
        for candidate in (current_owner, fence_owner, floor_owner):
            if candidate is not None and not _same_local_prefix(authority, candidate):
                raise MeshPairingServiceError(
                    "not_orphaned",
                    "target pairing fence records disagree about the orphaned prefix",
                )

        # A crash can have written the operational fence's released transition
        # before its current-tail copy.  Do not drive that exact owner back to
        # held: same-pair released -> held is intentionally illegal.  Writing
        # the released successor directly makes the trailing index catch up.
        # If another pairing still owns the operational fence, install this
        # blank-prefix owner as held first; that is the sole permitted
        # released-other-pair -> held replacement in the store state machine.
        if signed is not None and fence_owner is None:
            held = TargetPairingFenceAuthority(
                pair_id=authority.pair_id,
                protocol_version=authority.protocol_version,
                phase="held",
                space_id=authority.space_id,
                source_public_key=authority.source_public_key,
                source_fingerprint=authority.source_fingerprint,
                target_public_key=authority.target_public_key,
                target_fingerprint=authority.target_fingerprint,
                invitation_digest=authority.invitation_digest,
                requested_scopes_digest=authority.requested_scopes_digest,
                base_epoch=authority.base_epoch,
                target_activation_receipt=None,
                source_activation_receipt=None,
                target_terminal_confirmation=None,
                issued_at_ms=self._clock_ms(),
            )
            await self._put_target_pairing_fence_and_current_tail(
                SignedTargetPairingFenceAuthority.sign(
                    held, self._config.private_key
                ),
                replace_settled=True,
            )
        released = TargetPairingFenceAuthority(
            pair_id=authority.pair_id,
            protocol_version=authority.protocol_version,
            phase="released",
            space_id=authority.space_id,
            source_public_key=authority.source_public_key,
            source_fingerprint=authority.source_fingerprint,
            target_public_key=authority.target_public_key,
            target_fingerprint=authority.target_fingerprint,
            invitation_digest=authority.invitation_digest,
            requested_scopes_digest=authority.requested_scopes_digest,
            base_epoch=authority.base_epoch,
            target_activation_receipt=None,
            source_activation_receipt=None,
            target_terminal_confirmation=None,
            issued_at_ms=self._clock_ms(),
        )
        signed_released = SignedTargetPairingFenceAuthority.sign(
            released, self._config.private_key
        )
        # A held operational fence can survive loss of the floor.  Restore the
        # durable discriminator from this exact owner before the raw release;
        # a later ordinary write then observes a complete released shape.
        await self.store.put_target_pairing_protocol_floor(signed_released)
        await self._put_target_pairing_fence_and_current_tail(signed_released)
        await self.store.put_target_pairing_admission_anchor(
            SignedTargetPairingAdmissionAnchor.sign(
                TargetPairingAdmissionAnchor(
                    protocol_version=1,
                    space_id=authority.space_id,
                    target_public_key=authority.target_public_key,
                    target_fingerprint=authority.target_fingerprint,
                    issued_at_ms=self._clock_ms(),
                ),
                self._config.private_key,
            )
        )

    async def assert_space_not_reserved(self, space_id: str) -> None:
        """O(1), fail-closed ordinary-write guard for target pairing tails.

        Legacy spaces retain the raw-reservation compatibility path.  Once a
        local target has entered the #417 fence protocol, a direct signed
        per-space fence is mandatory: its loss cannot re-enable a write by
        scanning (or merely failing to find) historic operational sessions.
        """

        await self.store.assert_space_not_reserved(space_id)
        admission_anchor, protocol_floor, current_tail, fence = await asyncio.gather(
            self.store.get_target_pairing_admission_anchor(space_id),
            self.store.get_target_pairing_protocol_floor(space_id),
            self.store.get_target_pairing_current_tail(space_id),
            self.store.get_target_pairing_fence(space_id),
        )
        try:
            if admission_anchor is not None:
                admission_anchor.verify(self._config.public_key)
                anchor = admission_anchor.anchor
                if (
                    anchor.space_id != space_id
                    or anchor.target_fingerprint != self._config.fingerprint
                    or anchor.target_public_key != self._config.public_key
                ):
                    raise ValueError("target pairing admission anchor does not bind this instance")
        except Exception as exc:
            raise MeshPairingStoreError(
                "space_reserved", "space target pairing admission anchor is unavailable"
            ) from exc
        if protocol_floor is None:
            # A current signed fence without its permanent discriminator is
            # loss/corruption, not a legacy target.  Treating that mixed shape
            # as legacy would make deletion of only the floor an ordinary-write
            # permission change.
            if admission_anchor is None and current_tail is None and fence is None:
                return
            raise MeshPairingStoreError(
                "space_reserved", "space target pairing protocol floor is unavailable"
            )
        try:
            protocol_floor.verify(self._config.public_key)
            floor = protocol_floor.authority
            if (
                floor.space_id != space_id
                or floor.target_fingerprint != self._config.fingerprint
                or floor.target_public_key != self._config.public_key
            ):
                raise ValueError("target pairing floor does not bind this instance")
            if current_tail is None or fence is None:
                raise ValueError("target pairing current fence authority is missing")
            current_tail.verify(self._config.public_key)
            fence.verify(self._config.public_key)
            if current_tail.canonical_bytes() != fence.canonical_bytes():
                raise ValueError("target pairing fence does not match its current tail")
            authority = fence.authority
            if (
                authority.space_id != space_id
                or authority.target_fingerprint != self._config.fingerprint
                or authority.target_public_key != self._config.public_key
            ):
                raise ValueError("target pairing fence does not bind this instance")
            if authority.phase == "held":
                raise MeshPairingStoreError(
                    "space_reserved",
                    "space is reserved for an incomplete Project Mesh pairing",
                )
            if authority.phase in ("terminal_confirmed", "released"):
                # Strict parsing and cryptographic cross-binding of terminal
                # evidence occur in TargetPairingFenceAuthority.__post_init__.
                # This guard deliberately performs no session inventory scan,
                # receipt lookup, or retained bootstrap parse.
                return
            raise ValueError("unknown target pairing fence phase")
        except MeshPairingStoreError:
            raise
        except Exception as exc:
            raise MeshPairingStoreError(
                "space_reserved", "space target pairing authority is unavailable"
            ) from exc

    async def assert_no_pairing_activation(
        self, space_id: str, ignore_pair_id: Optional[str] = None
    ) -> None:
        """Refuse an epoch-advancing membership mutation while a SOURCE pairing
        for ``space_id`` is mid-activation.

        ``ignore_pair_id`` is the pairing-scoped bypass: a pairing driving its OWN
        Transition-1/2 or give-up (admit/promote/remove/evict) passes its
        ``pair_id`` so this ignores its own session (it IS the activation) while
        still refusing when a DIFFERENT pairing for the space is mid-activation
        (which its epoch bump would split). Operator paths (add_member,
        update_member_scopes, apply_membership_plan, unsafe backup recovery) pass
        ``None`` and are fenced against every mid-activation pairing.

        Between Transition 2 (the atomic e+1 -> e+2 promotion) and the confirmed
        target activation, the source holds a pre-computed e+2 activation event and
        digest but no membership lock over the network delivery. An operator
        epoch-advancing mutation (``update_member_scopes`` / ``add_member`` /
        ``apply_membership_plan``) in that window would advance the source past e+2
        while the target self-promotes to the stale pre-computed e+2, splitting the
        two ``MembershipView``\\ s. The membership layer calls this (under its own
        ``_space_lock``) before such a mutation; the mesh registers it as the
        activation checker at startup.

        The mid-activation SOURCE states are ``awaiting_acks`` (the intent is
        persisted BEFORE the promotion, so this happens-before any post-promotion
        rescope that grabs the membership lock) and ``blocked_recovery`` (a
        promoted-but-unconfirmed pairing that ``resume`` will re-drive at the same
        fixed e+2). Reads storage only (no lock), so it never deadlocks against the
        held membership lock. The give-up primitives (``remove_pending_candidate``,
        ``evict_member``) are fenced too, by default; only the OWNING pairing's
        give-up bypasses (via ``ignore_pair_id``), so an operator clears a stuck
        pairing through ``evict`` / ``force_evict_member`` (which supply the owning
        ``pair_id``), never a raw unfenced membership call. A single-in-flight-
        pairing-per-source gate (in ``create_invitation`` / ``approve``) guarantees
        there is never a DIFFERENT in-flight pairing to fence the owner, so the
        give-up always converges.

        New flows use one targeted durable activation-fence record. If absent,
        migration resolves only membership-bound incarnations and then persists
        a per-space sentinel; append-only session history is never authority."""

        # This is a direct, signed per-space authority index for a #417 source
        # tail.  It is written with the exported bootstrap evidence before e+2
        # and removed only after the target's detached terminal confirmation
        # readbacks.  Therefore deleting/retyping a member incarnation or the
        # mutable activation-fence object cannot reclassify an unfinished tail
        # as legacy history.
        membership = await self._hive_store(space_id).get_membership()
        protocol_floor = await self.store.get_source_activation_protocol_floor(
            space_id
        )
        if protocol_floor is not None:
            try:
                protocol_floor.verify(self._config.public_key)
            except Exception as exc:
                raise PairingActivationError(space_id) from exc
            floor_authority = protocol_floor.authority
            if (
                floor_authority.space_id != space_id
                or floor_authority.source_fingerprint != self._config.fingerprint
            ):
                raise PairingActivationError(space_id)
        marker = await self.store.get_source_activation_marker(space_id)
        if marker is not None:
            marker.verify(self._config.public_key)
            marker_pair_id = marker.evidence.pair_id
            marker_session = await self.store.get_session(marker_pair_id)
            if (
                marker_session is None
                or marker_session.pair_id != marker_pair_id
                or marker_session.role != MeshPairingRole.SOURCE.value
                or marker_session.space_id != space_id
            ):
                raise PairingActivationError(space_id)
            if not await self._source_activation_marker_matches_current_tail(
                marker, marker_session, membership
            ):
                raise PairingActivationError(space_id)
            if marker_session.state == MeshPairingState.ACTIVE.value:
                if not await self._source_terminal_confirmation_matches(
                    marker_session
                ):
                    raise PairingActivationError(space_id)
                await self.store.release_source_activation_marker(
                    space_id, marker_pair_id
                )
            elif marker_session.state == MeshPairingState.CANCELLED.value:
                await self.store.release_source_activation_marker(
                    space_id, marker_pair_id
                )
            elif marker_session.state in (
                *_SOURCE_MUTATING_STATES,
                MeshPairingState.APPROVED.value,
            ):
                if marker_pair_id != ignore_pair_id:
                    raise PairingActivationError(space_id)
                # The owning pairing is performing its own e+1/e+2 or recovery
                # transition under the lifecycle's stronger locks.  APPROVED is
                # included only for the hard-crash prefix above: it is already
                # bound to the exact PENDING e+1 candidate by the signed marker.
                return
            else:
                raise PairingActivationError(space_id)

        pending_members: list[Member] = []
        if membership is not None:
            for member in membership.members:
                if member.status != MemberStatus.PENDING.value:
                    continue
                pending_members.append(member)
                if (
                    ignore_pair_id is not None
                    and member.incarnation == ignore_pair_id
                ):
                    continue
                raise PairingActivationError(space_id)

        fence_record = await self.store.get_activation_fence_record(space_id)
        if fence_record is not None:
            fence_pair_id, fence_phase = fence_record
            migration_entry = await self.store.get_activation_migration_entry(
                space_id
            )
            if migration_entry is None:
                # The existing fence is already a targeted authority index. A
                # single durable migration record makes that fact survive the
                # terminal fence deletion without rescanning append-only history.
                await self.store.put_activation_migration(
                    space_id, fence_pair_id, now_ms=self._clock_ms()
                )
                migration_owner = fence_pair_id
                migration_is_source_tail = False
                migration_authority = None
            else:
                (
                    migration_owner,
                    migration_is_source_tail,
                    migration_authority,
                ) = migration_entry
            if protocol_floor is not None and not migration_is_source_tail:
                raise PairingActivationError(space_id)
            if migration_owner not in ("", fence_pair_id):
                raise PairingActivationError(space_id)
            session = await self.store.get_session(fence_pair_id)
            if (
                session is None
                or session.role != MeshPairingRole.SOURCE.value
                or session.space_id != space_id
                or session.pair_id != fence_pair_id
            ):
                raise PairingActivationError(space_id)
            if migration_is_source_tail and not self._source_activation_migration_matches_session(
                migration_authority, session
            ):
                raise PairingActivationError(space_id)
            # The lifecycle writes the terminal session before deleting the
            # fence.  A crash in that narrow tail must converge on restart,
            # otherwise an already-settled pairing would fence the space
            # forever.  Only the two terminal states reachable after a fence
            # has been armed are accepted; every other terminal/corrupt state
            # remains fail-closed.
            if session.state in (
                MeshPairingState.ACTIVE.value,
                MeshPairingState.CANCELLED.value,
            ):
                if (
                    session.state == MeshPairingState.ACTIVE.value
                    and (
                        migration_is_source_tail
                        or await self._is_new_source_terminal_tail(session)
                    )
                    and not await self._source_terminal_confirmation_matches(session)
                ):
                    # The mutable fence phase is diagnostic only.  New protocol
                    # source ACTIVE remains fenced until the target's detached
                    # signed readback is present, even if a raw storage rewrite
                    # changes/removes the phase before this check.
                    raise PairingActivationError(space_id)
                if migration_owner == fence_pair_id and not migration_is_source_tail:
                    await self.store.put_activation_migration(
                        space_id, "", now_ms=self._clock_ms()
                    )
                await self.store.release_activation_fence(
                    space_id, fence_pair_id
                )
                await self.store.release_source_activation_marker(
                    space_id, fence_pair_id
                )
                return
            if session.state == MeshPairingState.APPROVED.value:
                if (
                    migration_owner == ignore_pair_id
                    and (
                        # The source re-arms its bounded owner immediately
                        # after persisting APPROVED, before Transition 1.  The
                        # lifecycle invokes this guard for that owner's exact
                        # admission while no PENDING member exists yet.
                        not pending_members
                        or (
                            len(pending_members) == 1
                            and pending_members[0].incarnation == migration_owner
                        )
                    )
                ):
                    return
                raise PairingActivationError(space_id)
            if session.state not in (
                MeshPairingState.TRANSFERRING.value,
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
            ):
                raise PairingActivationError(space_id)
            if fence_pair_id != ignore_pair_id:
                raise PairingActivationError(space_id)
            return

        migration_entry = await self.store.get_activation_migration_entry(space_id)
        if migration_entry is not None:
            (
                migration_owner,
                migration_is_source_tail,
                migration_authority,
            ) = migration_entry
            if protocol_floor is not None and not migration_is_source_tail:
                raise PairingActivationError(space_id)
            if migration_owner == "":
                await self._assert_new_source_terminal_tails_confirmed(
                    space_id, membership
                )
                return
            session = await self.store.get_session(migration_owner)
            if (
                session is None
                or session.role != MeshPairingRole.SOURCE.value
                or session.space_id != space_id
                or session.pair_id != migration_owner
            ):
                raise PairingActivationError(space_id)
            if migration_is_source_tail and not self._source_activation_migration_matches_session(
                migration_authority, session
            ):
                raise PairingActivationError(space_id)
            if session.state in (
                MeshPairingState.ACTIVE.value,
                MeshPairingState.CANCELLED.value,
            ):
                if (
                    session.state == MeshPairingState.ACTIVE.value
                    and (
                        migration_is_source_tail
                        or await self._is_new_source_terminal_tail(session)
                    )
                    and not await self._source_terminal_confirmation_matches(session)
                ):
                    raise PairingActivationError(space_id)
                if not migration_is_source_tail:
                    await self.store.put_activation_migration(
                        space_id, "", now_ms=self._clock_ms()
                    )
                return
            if session.state == MeshPairingState.APPROVED.value:
                if (
                    migration_owner == ignore_pair_id
                    and (
                        # See the fence branch above: this is the owner-only
                        # pre-Transition-1 admission window, never permission
                        # for an unrelated external membership mutation.
                        not pending_members
                        or (
                            len(pending_members) == 1
                            and pending_members[0].incarnation == migration_owner
                        )
                    )
                ):
                    return
                raise PairingActivationError(space_id)
            if session.state not in (
                MeshPairingState.TRANSFERRING.value,
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
            ):
                raise PairingActivationError(space_id)
            if migration_owner != ignore_pair_id:
                raise PairingActivationError(space_id)
            return

        # Once this source has entered #417, absence of its signed per-space
        # owner index is critical-state loss, not permission to fall back to
        # the old discovery heuristic.  The immutable protocol floor remains
        # after terminal completion specifically to make deletion/downgrade of
        # that current index fail closed.
        if protocol_floor is not None:
            raise PairingActivationError(space_id)

        active = active_members(membership) if membership is not None else []

        # Reaching this branch with PENDING means it is the caller's explicitly
        # ignored incarnation. Resolve that one session directly; a missing or
        # malformed incarnation can never authorize a membership mutation.
        if pending_members:
            if len(pending_members) != 1 or not pending_members[0].incarnation:
                raise PairingActivationError(space_id)
            pending_owner = pending_members[0].incarnation
            pending_session = await self.store.get_session(pending_owner)
            if (
                pending_session is None
                or pending_session.pair_id != pending_owner
                or pending_session.role != MeshPairingRole.SOURCE.value
                or pending_session.space_id != space_id
                or pending_session.state
                not in (
                    *_SOURCE_MUTATING_STATES,
                    MeshPairingState.APPROVED.value,
                )
            ):
                raise PairingActivationError(space_id)
            await self.store.put_activation_migration(
                space_id, pending_owner, now_ms=self._clock_ms()
            )
            return

        # Upgrade fast-path: under the caller-held membership lock, one ACTIVE
        # member and no PENDING member proves that no legacy activation can be
        # between its shared transitions. Before promotion it would expose a
        # PENDING member; after promotion it would expose a second ACTIVE member.
        # Persist that bounded proof instead of scanning append-only terminal
        # session history, which may already exceed the legacy inventory cap.
        if (
            membership is not None
            and len(active) == 1
        ):
            await self.store.put_activation_migration(
                space_id, "", now_ms=self._clock_ms()
            )
            return

        # Resolve every incarnation that is present in the authoritative head.
        # Incarnation-less ACTIVE members can legitimately predate P10 and do
        # not encode a hidden Mesh activation; malformed referenced sessions do.
        incarnation_sessions: list[MeshPairingSession] = []
        if membership is not None and len(active) > 1:
            for member in active:
                if not member.incarnation:
                    continue
                session = await self.store.get_session(member.incarnation)
                if (
                    session is None
                    or session.pair_id != member.incarnation
                    or session.space_id != space_id
                    or (
                        session.role == MeshPairingRole.SOURCE.value
                        and session.state
                        not in (
                            MeshPairingState.TRANSFERRING.value,
                            MeshPairingState.AWAITING_ACKS.value,
                            MeshPairingState.BLOCKED_RECOVERY.value,
                            MeshPairingState.ACTIVE.value,
                        )
                    )
                    or (
                        session.role == MeshPairingRole.TARGET.value
                        and session.state != MeshPairingState.ACTIVE.value
                    )
                    or session.role
                    not in (
                        MeshPairingRole.SOURCE.value,
                        MeshPairingRole.TARGET.value,
                    )
                ):
                    raise PairingActivationError(space_id)
                incarnation_sessions.append(session)
            mutating = [
                session
                for session in incarnation_sessions
                if session.role == MeshPairingRole.SOURCE.value
                and session.state in _SOURCE_MUTATING_STATES
            ]
            if len(mutating) > 1:
                raise PairingActivationError(space_id)
            if mutating:
                await self.store.put_activation_migration(
                    space_id, mutating[0].pair_id, now_ms=self._clock_ms()
                )
                if mutating[0].pair_id != ignore_pair_id:
                    raise PairingActivationError(space_id)
                return

            await self._assert_new_source_terminal_tails_confirmed(
                space_id, membership
            )

        if membership is None:
            # No shared membership exists for this space, hence no source-side
            # activation transition can be in flight.
            return
        if not active:
            raise PairingActivationError(space_id)
        # Incarnation-less ACTIVE members predate Mesh pairing and cannot encode
        # a hidden P10 activation. Every member that *does* carry an incarnation
        # was checked by targeted get above, so global append-only history is not
        # an authority input and can never impose a permanent session ceiling.
        await self.store.put_activation_migration(
            space_id, "", now_ms=self._clock_ms()
        )

    # ==================================================================
    # Existing-space source readiness + explicit preparation (#413)
    # ==================================================================

    @staticmethod
    def _model_payload(model: Any) -> dict[str, Any]:
        return model.model_dump(mode="json")

    def _source_genesis_models(
        self, intent: SourcePreparationIntent
    ) -> dict[str, Any]:
        """Rebuild the exact genesis frozen by a durable preparation intent."""

        member = Member(
            node_id=intent.node_id,
            display_name=intent.display_name,
            endpoint=intent.public_url,
            public_key=intent.membership_public_key,
            joined_at=intent.started_at_iso,
            status=MemberStatus.ACTIVE,
            scopes=[
                PeerScope.READ.value,
                PeerScope.PROPOSE.value,
                PeerScope.COMMIT.value,
            ],
        )
        return {
            "unsafe": NodeHealth(
                status=HiveNodeStatus.UNSAFE,
                reason=_SOURCE_INITIALIZATION_REASON,
                updated_at=intent.started_at_iso,
            ),
            "healthy": NodeHealth(
                status=HiveNodeStatus.HEALTHY,
                reason=_SOURCE_READY_REASON,
                updated_at=intent.started_at_iso,
            ),
            "node": NodeIdentity(
                node_id=intent.node_id,
                display_name=intent.display_name,
                public_key=intent.membership_public_key,
                created_at=intent.started_at_iso,
            ),
            "membership": MembershipView(
                epoch=0,
                members=[member],
                updated_at=intent.started_at_iso,
            ),
            "term": TermState(
                term=0,
                updated_by_node_id=intent.node_id,
                updated_at=intent.started_at_iso,
            ),
            "token": TokenLeaseState(
                state=TokenState.FREE,
                holder_node_id=None,
                term=0,
                fencing_token=0,
                membership_epoch=0,
                bank_version=-1,
            ),
            "pointer": BankVersionPointer(
                bank_version=-1,
                commit_id="",
                updated_at=intent.started_at_iso,
            ),
        }

    def _preparation_binding_matches(
        self, space_id: str, intent: SourcePreparationIntent
    ) -> bool:
        return (
            intent.space_id == space_id
            and intent.source_fingerprint == self._config.fingerprint
            and intent.membership_public_key
            == _legacy_membership_key(self._config.public_key)
            and intent.node_id == _node_id_from_fingerprint(self._config.fingerprint)
            and intent.display_name == self._config.display_name
            and intent.public_url == self._config.public_url
        )

    async def _preparation_progress(
        self,
        space_id: str,
        intent: SourcePreparationIntent,
        *,
        objects: Optional[list[dict]] = None,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Return ``(exact, phase, expected)`` for an active intent.

        ``phase`` is ``empty``/``unsafe``/``node``/``membership``/``term``/
        ``token``/``pointer``/``healthy`` or ``conflict``. Only an ordered exact
        prefix is resumable; no divergent object is ever repaired here.
        """

        expected = self._source_genesis_models(intent)
        store = self._hive_store(space_id)
        ordered = [
            ("unsafe", layout.node_status_key(space_id), store.get_node_status),
            ("node", layout.node_key(space_id), store.get_node_identity),
            ("membership", layout.members_key(space_id), store.get_membership),
            ("term", layout.term_key(space_id), store.get_term),
            ("token", layout.token_key(space_id), store.get_token),
            (
                "pointer",
                layout.bank_version_key(space_id),
                store.get_bank_version_pointer,
            ),
        ]
        if objects is None:
            objects = await self._storage_factory().list_objects(
                layout.HIVEMIND_PREFIX(space_id),
                max_keys=_PREPARATION_MAX_HIVEMIND_KEYS + 1,
            )
        if len(objects) > _PREPARATION_MAX_HIVEMIND_KEYS:
            return False, "conflict", expected
        observed_keys = {
            obj.get("Key") for obj in objects if isinstance(obj, dict)
        }
        allowed_keys = {key for _, key, _ in ordered}
        if any(type(key) is not str or key not in allowed_keys for key in observed_keys):
            return False, "conflict", expected

        present_indices = [
            index for index, (_, key, _) in enumerate(ordered) if key in observed_keys
        ]
        if present_indices:
            highest = max(present_indices)
            if set(present_indices) != set(range(highest + 1)):
                return False, "conflict", expected
        else:
            return True, "empty", expected

        health_is_healthy = False
        for index in present_indices:
            name, _key, getter = ordered[index]
            try:
                actual = await getter()
            except Exception:
                return False, "conflict", expected
            if actual is None:
                return False, "conflict", expected
            if name == "unsafe":
                if self._model_payload(actual) == self._model_payload(expected["healthy"]):
                    health_is_healthy = True
                elif self._model_payload(actual) != self._model_payload(expected["unsafe"]):
                    return False, "conflict", expected
            elif self._model_payload(actual) != self._model_payload(expected[name]):
                return False, "conflict", expected

        if health_is_healthy:
            if len(present_indices) != len(ordered):
                return False, "conflict", expected
            return True, "healthy", expected
        return True, ordered[max(present_indices)][0], expected

    @staticmethod
    def _source_message(state: str) -> str:
        messages = {
            "local_only_can_prepare": "This local space can be prepared for Project Mesh.",
            "preparing": "Source preparation can resume from its exact durable intent.",
            "prepare_recovery_required": "Source preparation state diverged; automatic recovery is refused.",
            "ready": "This space is ready to create a Project Mesh invitation.",
            "busy": "A same-space maintenance job is still active or queued.",
            "pairing_in_flight": "A membership-changing Project Mesh pairing is in progress.",
            "mutation_in_progress": "The source token is held or releasing; wait for the mutation to finish.",
            "insufficient_scope": "The configured source member does not have commit scope.",
            "multi_member": "Project Mesh V1 invitations require one active-member source.",
            "identity_mismatch": "The space identity does not match this Project Mesh instance.",
            "unavailable": "This source could not be inspected; retry when its storage is available.",
            "unsafe": "The source state is unsafe and cannot be prepared automatically.",
            "resync_required": "The source requires resynchronization before pairing.",
            "not_a_space": "The selected id is not a committed space.",
        }
        return messages[state]

    def _source_projection(
        self,
        *,
        space_id: str,
        observation: str,
        committed: str = "",
        committed_reason: str = "",
        object_keys=(),
        preparation: SourcePreparationIntent | None = None,
        reservation: str = "",
        lane: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the fixed-size HCJ projection used by readiness tokens.

        Every variable collection/model is reduced through domain-separated,
        length-framed SHA-256 first. The final HCJ object therefore has a fixed
        field count regardless of Hivemind history, membership size, or queue
        depth. Pairing history is deliberately excluded from readiness; the
        stable empty digest below preserves the existing projection schema and
        state-token bytes.
        """

        lane = lane or {}
        running = lane.get("running_job") or {}
        running_id = lane.get("running_job_id", "")
        if not running_id and isinstance(running, dict):
            running_id = running.get("job_id", "")
        queued_ids = lane.get("queued_job_ids", [])
        if not isinstance(queued_ids, list):
            queued_ids = []
        preparation_records = (
            [preparation.canonical_bytes()] if preparation is not None else []
        )
        return {
            "projection_version": _STATE_TOKEN_VERSION,
            "space_id": space_id,
            "observation": observation,
            "committed": committed,
            "committed_reason_digest": _framed_digest(
                "committed-reason", [committed_reason.encode("utf-8")]
            ),
            "hivemind_keys_digest": _framed_digest(
                "hivemind-keys", (key.encode("utf-8") for key in object_keys)
            ),
            "public_profile_digest": _framed_digest(
                "public-profile",
                [
                    _framed_record(
                        self._config.fingerprint.encode("utf-8"),
                        self._config.public_key.encode("utf-8"),
                        self._config.public_url.encode("utf-8"),
                        self._config.display_name.encode("utf-8"),
                    )
                ],
            ),
            "preparation_digest": _framed_digest(
                "preparation", preparation_records
            ),
            "reservation_digest": _framed_digest(
                "reservation", [reservation.encode("utf-8")]
            ),
            "source_sessions_digest": _framed_digest("source-sessions", []),
            "lane_digest": _framed_digest(
                "lane",
                [
                    _framed_record(
                        str(running_id).encode("utf-8"),
                        str(lane.get("queued_count", 0)).encode("ascii"),
                        *(
                            str(job_id).encode("utf-8")
                            for job_id in queued_ids
                        ),
                    )
                ],
            ),
            "preparation_phase": "",
            "critical_state_digest": _framed_digest("critical-state", []),
        }

    @staticmethod
    def _critical_state_digest(**models: Any) -> str:
        records = []
        for name in ("node", "membership", "health", "term", "token", "pointer"):
            model = models.get(name)
            payload = b""
            if model is not None:
                payload = json.dumps(
                    model.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            records.append(_framed_record(name.encode("ascii"), payload))
        return _framed_digest("critical-state", records)

    def _source_result(
        self,
        *,
        space_id: str,
        state: str,
        hive_status: str,
        projection: dict[str, Any],
        source_ready: bool = False,
        source_initializable: bool = False,
        can_create_invitation: bool = False,
        resumable: bool = False,
    ) -> dict[str, Any]:
        # ``projection`` is fixed-size and digest-only for all variable state.
        state_token = hashlib.sha256(
            canonical_dumps({"state": state, "projection": projection})
        ).hexdigest()
        return {
            "space_id": space_id,
            "state": state,
            "source_ready": source_ready,
            "source_initializable": source_initializable,
            "can_create_invitation": can_create_invitation,
            "resumable": resumable,
            "hive_status": hive_status,
            "reason_code": state,
            "message": self._source_message(state),
            "state_token": state_token,
        }

    async def inspect_source_eligibility(self, space_id: str) -> dict[str, Any]:
        """Read one authoritative Project Mesh source-readiness projection."""

        # Targeted readiness does not need the process-wide pairing history.
        # Membership is authoritative for every shared mutation state, while
        # pre-mutation ISSUED/CLAIMED sessions intentionally remain nonblocking.
        return await self._inspect_source_eligibility(space_id)

    async def _readiness_critical_head_is_bounded(
        self, storage: Any, space_id: str
    ) -> bool | None:
        """Prove singleton Size metadata before any readiness model GET.

        Mature event/ACK history stays unscanned. Only the six authoritative
        singleton paths are exact-prefix listed, each with a two-key sentinel;
        malformed/missing Size or prefix collisions fail closed.
        """

        bounds = {
            layout.node_key(space_id): _READINESS_MAX_SINGLETON_BYTES,
            layout.members_key(space_id): _READINESS_MAX_MEMBERS_BYTES,
            layout.node_status_key(space_id): _READINESS_MAX_SINGLETON_BYTES,
            layout.term_key(space_id): _READINESS_MAX_SINGLETON_BYTES,
            layout.token_key(space_id): _READINESS_MAX_SINGLETON_BYTES,
            layout.bank_version_key(space_id): _READINESS_MAX_SINGLETON_BYTES,
        }

        async def probe(key: str, max_bytes: int) -> bool | None:
            try:
                objects = await storage.list_objects(key, max_keys=2)
                if len(objects) > 1:
                    return False
                if not objects:
                    return True
                obj = objects[0]
                if (
                    not isinstance(obj, dict)
                    or obj.get("Key") != key
                    or type(obj.get("Size")) is not int
                    or obj["Size"] < 0
                    or obj["Size"] > max_bytes
                ):
                    return False
                return True
            except Exception:
                # Metadata transport failure is not malformed persisted state.
                # Keep it distinct so one transient backend outage is never
                # projected as an operator-actionable ``unsafe`` source.
                return None

        results = await asyncio.gather(
            *(probe(key, max_bytes) for key, max_bytes in bounds.items())
        )
        return None if any(result is None for result in results) else all(results)

    async def _readiness_product_head_is_bounded(
        self, storage: Any, space_id: str
    ) -> bool | None:
        """Bound product commit markers and preparation evidence before GET.

        ``classify_committed_state`` reads these product objects directly, and
        the preparation store reads its fingerprint-neutral record directly.
        Exact-key metadata probes keep the status/readiness surface from
        loading an attacker-sized payload once per listed space.
        """

        bounds = {
            f"{space_id}/_meta.json": (_READINESS_MAX_PRODUCT_BYTES, None),
            f"{space_id}/_rules.md": (_READINESS_MAX_PRODUCT_BYTES, None),
            f"{space_id}/live/.keep": (0, 0),
            f"{space_id}/bank/.keep": (0, 0),
            source_preparation_key(space_id): (
                _READINESS_MAX_PREPARATION_BYTES,
                None,
            ),
        }

        async def probe(
            key: str, max_bytes: int, exact_bytes: int | None
        ) -> bool | None:
            try:
                objects = await storage.list_objects(key, max_keys=2)
                if len(objects) > 1:
                    return False
                if not objects:
                    return True
                obj = objects[0]
                size = obj.get("Size") if isinstance(obj, dict) else None
                if (
                    not isinstance(obj, dict)
                    or obj.get("Key") != key
                    or type(size) is not int
                    or size < 0
                    or size > max_bytes
                    or (exact_bytes is not None and size != exact_bytes)
                ):
                    return False
                return True
            except Exception:
                # See the critical-head counterpart: availability is not
                # persistence corruption and must remain a separate state.
                return None

        results = await asyncio.gather(
            *(
                probe(key, max_bytes, exact_bytes)
                for key, (max_bytes, exact_bytes) in bounds.items()
            )
        )
        return None if any(result is None for result in results) else all(results)

    async def _readiness_classify_committed_state(
        self, storage: Any, space_id: str
    ) -> tuple[str, str] | None:
        """Classify product state through the shared detailed classifier.

        ``SpaceService.inspect_committed_state`` owns the constitutive marker,
        rules and sentinel contract.  Mesh alone projects a temporary backend
        failure as ``None`` so the caller can return the closed
        ``unavailable`` readiness state rather than mislabelling it as stored
        corruption.
        """

        state, reason = await SpaceService.inspect_committed_state(storage, space_id)
        return None if state == "unavailable" else (state, reason)

    async def _readiness_commit_is_bounded(
        self, storage: Any, space_id: str, bank_version: int
    ) -> bool | None:
        """Prove the pointer-selected commit Size before its model GET."""

        key = layout.commit_key(space_id, bank_version)
        try:
            objects = await storage.list_objects(key, max_keys=2)
        except Exception:
            # ``None`` is deliberately distinct from malformed metadata: an
            # unavailable backend must not be relabelled persisted corruption.
            return None
        if len(objects) != 1:
            return False
        obj = objects[0]
        size = obj.get("Size") if isinstance(obj, dict) else None
        return bool(
            isinstance(obj, dict)
            and obj.get("Key") == key
            and type(size) is int
            and 0 <= size <= _READINESS_MAX_SINGLETON_BYTES
        )

    async def _inspect_source_eligibility(self, space_id: str) -> dict[str, Any]:
        """Inspect one source from bounded authority-head state."""

        if type(space_id) is not str or _SOURCE_SPACE_ID_RE.fullmatch(space_id) is None:
            return self._source_result(
                space_id=space_id if type(space_id) is str else "",
                state="not_a_space",
                hive_status="not_a_space",
                projection=self._source_projection(
                    space_id="", observation="not_a_space"
                ),
            )

        storage = self._storage_factory()
        product_head_bounded = await self._readiness_product_head_is_bounded(
            storage, space_id
        )
        if product_head_bounded is None:
            return self._source_result(
                space_id=space_id,
                state="unavailable",
                hive_status="unavailable",
                projection=self._source_projection(
                    space_id=space_id, observation="product_head_unavailable"
                ),
            )
        if not product_head_bounded:
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=self._source_projection(
                    space_id=space_id, observation="product_head_unbounded"
                ),
            )
        try:
            classified = await self._readiness_classify_committed_state(
                storage, space_id
            )
            if classified is None:
                return self._source_result(
                    space_id=space_id,
                    state="unavailable",
                    hive_status="unavailable",
                    projection=self._source_projection(
                        space_id=space_id, observation="product_state_unavailable"
                    ),
                )
            committed, committed_reason = classified
            # One key is enough to distinguish local-only from an established
            # or residual Hivemind prefix. Mature sources may legitimately have
            # tens of thousands of history objects; readiness validates only
            # their bounded authoritative head, never inventories that history.
            objects = await storage.list_objects(
                layout.HIVEMIND_PREFIX(space_id),
                max_keys=1,
            )
        except Exception:
            return self._source_result(
                space_id=space_id,
                state="unavailable",
                hive_status="unavailable",
                projection=self._source_projection(
                    space_id=space_id, observation="source_inventory_unavailable"
                ),
            )
        object_keys = sorted(
            key
            for key in (obj.get("Key") for obj in objects if isinstance(obj, dict))
            if isinstance(key, str)
        )

        try:
            preparation = await self.store.get_source_preparation(space_id)
            reservation = await self.store.get_reservation(space_id)
        except Exception:
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=self._source_projection(
                    space_id=space_id,
                    observation="mesh_local_state_unreadable",
                    committed=committed,
                    committed_reason=committed_reason,
                    object_keys=object_keys,
                ),
            )
        if (
            preparation is not None
            and preparation.state_enum is SourcePreparationState.PREPARING
        ):
            try:
                objects = await storage.list_objects(
                    layout.HIVEMIND_PREFIX(space_id),
                    max_keys=_PREPARATION_MAX_HIVEMIND_KEYS + 1,
                )
            except Exception:
                return self._source_result(
                    space_id=space_id,
                    state="unavailable",
                    hive_status="unavailable",
                    projection=self._source_projection(
                        space_id=space_id,
                        observation="source_inventory_unavailable",
                        committed=committed,
                        committed_reason=committed_reason,
                    ),
                )
            object_keys = sorted(
                key
                for key in (
                    obj.get("Key") for obj in objects if isinstance(obj, dict)
                )
                if isinstance(key, str)
            )
        lane = await self._consolidation_queue.get_space_readiness_summary(space_id)
        lane_busy = bool(lane.get("running_job_id")) or bool(
            lane.get("queued_count", 0)
        )
        projection = self._source_projection(
            space_id=space_id,
            observation="observed",
            committed=committed,
            committed_reason=committed_reason,
            object_keys=object_keys,
            preparation=preparation,
            reservation=reservation or "",
            lane=lane,
        )

        if committed != "committed":
            if preparation is not None:
                state = (
                    "prepare_recovery_required"
                    if preparation.state_enum is SourcePreparationState.PREPARING
                    else "unsafe"
                )
            else:
                state = (
                    "not_a_space"
                    if committed == "absent" and not object_keys
                    else "unsafe"
                )
            return self._source_result(
                space_id=space_id,
                state=state,
                hive_status="not_a_space" if state == "not_a_space" else "unsafe",
                projection=projection,
            )

        critical_head_bounded = (
            await self._readiness_critical_head_is_bounded(storage, space_id)
            if object_keys
            else True
        )
        if critical_head_bounded is None:
            return self._source_result(
                space_id=space_id,
                state="unavailable",
                hive_status="unavailable",
                projection=self._source_projection(
                    space_id=space_id, observation="critical_head_unavailable"
                ),
            )
        if not critical_head_bounded:
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=projection,
            )

        try:
            product_hive_status = await hive_status_label(storage, space_id)
        except CorruptedStateError:
            product_hive_status = "unsafe"
        except Exception:
            return self._source_result(
                space_id=space_id,
                state="unavailable",
                hive_status="unavailable",
                projection=self._source_projection(
                    space_id=space_id, observation="critical_state_unavailable"
                ),
            )

        prepared_complete = (
            preparation is not None
            and preparation.state_enum is SourcePreparationState.COMPLETE
        )
        if prepared_complete and not self._preparation_binding_matches(
            space_id, preparation
        ):
            return self._source_result(
                space_id=space_id,
                state="identity_mismatch",
                hive_status=product_hive_status,
                projection=projection,
            )

        if preparation is not None and (
            preparation.state_enum is SourcePreparationState.PREPARING
        ):
            if not self._preparation_binding_matches(space_id, preparation):
                return self._source_result(
                    space_id=space_id,
                    state="prepare_recovery_required",
                    hive_status=product_hive_status,
                    projection=projection,
                )
            exact, phase, _expected = await self._preparation_progress(
                space_id, preparation, objects=objects
            )
            projection["preparation_phase"] = phase
            if not exact:
                return self._source_result(
                    space_id=space_id,
                    state="prepare_recovery_required",
                    hive_status=product_hive_status,
                    projection=projection,
                )
            if reservation is not None:
                return self._source_result(
                    space_id=space_id,
                    state="pairing_in_flight",
                    hive_status=product_hive_status,
                    projection=projection,
                )
            if lane_busy:
                return self._source_result(
                    space_id=space_id,
                    state="busy",
                    hive_status=product_hive_status,
                    projection=projection,
                )
            return self._source_result(
                space_id=space_id,
                state="preparing",
                hive_status=product_hive_status,
                projection=projection,
                source_initializable=True,
                resumable=True,
            )

        if not object_keys:
            # A completed preparation with no Hivemind state is a forbidden
            # downgrade/corruption, never a fresh local conversion.
            if preparation is not None:
                return self._source_result(
                    space_id=space_id,
                    state="unsafe",
                    hive_status="unsafe",
                    projection=projection,
                )
            if reservation is not None:
                return self._source_result(
                    space_id=space_id,
                    state="pairing_in_flight",
                    hive_status=product_hive_status,
                    projection=projection,
                )
            if lane_busy:
                return self._source_result(
                    space_id=space_id,
                    state="busy",
                    hive_status=product_hive_status,
                    projection=projection,
                )
            return self._source_result(
                space_id=space_id,
                state="local_only_can_prepare",
                hive_status=product_hive_status,
                projection=projection,
                source_initializable=True,
            )

        store = self._hive_store(space_id)
        try:
            node, membership, health, term, token, pointer = await asyncio.gather(
                store.get_node_identity(),
                store.get_membership(),
                store.get_node_status(),
                store.get_term(),
                store.get_token(),
                store.get_bank_version_pointer(),
            )
        except CorruptedStateError:
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=projection,
            )
        except Exception:
            return self._source_result(
                space_id=space_id,
                state="unavailable",
                hive_status="unavailable",
                projection=self._source_projection(
                    space_id=space_id,
                    observation="critical_state_unavailable",
                ),
            )
        projection["critical_state_digest"] = self._critical_state_digest(
            node=node,
            membership=membership,
            health=health,
            term=term,
            token=token,
            pointer=pointer,
        )

        if prepared_complete and (
            health is None
            or health.status != HiveNodeStatus.HEALTHY.value
            or token is None
            or pointer is None
        ):
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=projection,
            )

        if health is not None and health.status == HiveNodeStatus.RESYNC_REQUIRED.value:
            return self._source_result(
                space_id=space_id,
                state="resync_required",
                hive_status="resync_required",
                projection=projection,
            )
        if health is not None and health.status != HiveNodeStatus.HEALTHY.value:
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=projection,
            )
        if node is None or membership is None or term is None:
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=projection,
            )
        active = active_members(membership)
        pending_member = any(
            member.status == MemberStatus.PENDING.value
            for member in membership.members
        )
        if len(active) != 1:
            state = "multi_member" if len(active) > 1 else "unsafe"
            return self._source_result(
                space_id=space_id,
                state=state,
                hive_status=product_hive_status,
                projection=projection,
            )
        source_member = active[0]
        try:
            configured_raw = decode_membership_public_key(self._config.public_key)
            node_raw = decode_membership_public_key(node.public_key)
            member_raw = decode_membership_public_key(source_member.public_key)
        except Exception:
            return self._source_result(
                space_id=space_id,
                state="identity_mismatch",
                hive_status=product_hive_status,
                projection=projection,
            )
        if (
            source_member.node_id != node.node_id
            or node_raw != member_raw
            or node_raw != configured_raw
        ):
            return self._source_result(
                space_id=space_id,
                state="identity_mismatch",
                hive_status=product_hive_status,
                projection=projection,
            )
        if prepared_complete and (
            source_member.endpoint != self._config.public_url
            or source_member.display_name != self._config.display_name
            or node.display_name != self._config.display_name
        ):
            return self._source_result(
                space_id=space_id,
                state="identity_mismatch",
                hive_status=product_hive_status,
                projection=projection,
            )
        if not source_member.has_scope(PeerScope.COMMIT):
            return self._source_result(
                space_id=space_id,
                state="insufficient_scope",
                hive_status=product_hive_status,
                projection=projection,
            )
        pointer_bank_version = pointer.bank_version if pointer is not None else -1
        if pointer is not None and (
            pointer.bank_version < -1
            or (pointer.bank_version == -1 and pointer.commit_id != "")
            or (pointer.bank_version >= 0 and not pointer.commit_id)
        ):
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=projection,
            )
        if token is not None and token.state in (
            TokenState.HELD.value,
            TokenState.RELEASING.value,
        ):
            return self._source_result(
                space_id=space_id,
                state="mutation_in_progress",
                hive_status=product_hive_status,
                projection=projection,
            )
        if token is not None and (
            token.term < 0
            or token.membership_epoch < 0
            or token.bank_version < -1
            or token.term > term.term
            or token.fencing_token > token.term
            or token.membership_epoch > membership.epoch
            or token.bank_version > pointer_bank_version
        ):
            # A FREE token is a retained release baseline and may legitimately
            # lag later term/membership/commit advances. Future values are the
            # unsafe case; active leases were handled above.
            return self._source_result(
                space_id=space_id,
                state="unsafe",
                hive_status="unsafe",
                projection=projection,
            )
        if pointer is not None and pointer.bank_version >= 0:
            commit_bounded = await self._readiness_commit_is_bounded(
                storage, space_id, pointer.bank_version
            )
            if commit_bounded is None:
                return self._source_result(
                    space_id=space_id,
                    state="unavailable",
                    hive_status="unavailable",
                    projection=self._source_projection(
                        space_id=space_id,
                        observation="selected_commit_unavailable",
                    ),
                )
            if not commit_bounded:
                return self._source_result(
                    space_id=space_id,
                    state="unsafe",
                    hive_status="unsafe",
                    projection=projection,
                )
            try:
                commit = await store.get_commit(pointer.bank_version)
            except CorruptedStateError:
                # The selected commit is part of the bounded authoritative
                # head, not a process-wide inventory failure. Confine exact
                # persisted-state corruption to this source: callers receive a
                # deterministic, digest-only unsafe projection and neighbours
                # remain inspectable.
                return self._source_result(
                    space_id=space_id,
                    state="unsafe",
                    hive_status="unsafe",
                    projection=projection,
                )
            except Exception:
                return self._source_result(
                    space_id=space_id,
                    state="unavailable",
                    hive_status="unavailable",
                    projection=self._source_projection(
                        space_id=space_id,
                        observation="selected_commit_unavailable",
                    ),
                )
            if commit is None or commit.commit_id != pointer.commit_id:
                return self._source_result(
                    space_id=space_id,
                    state="unsafe",
                    hive_status="unsafe",
                    projection=projection,
                )
        if reservation is not None:
            # A target reservation must never coexist with a valid source. Keep
            # the protocol state visible as ready but refuse invitation issuance
            # until the conflicting local pairing ownership is resolved.
            return self._source_result(
                space_id=space_id,
                state="pairing_in_flight",
                hive_status=product_hive_status,
                projection=projection,
                source_ready=True,
            )
        if pending_member:
            # Issuing another one-time invitation does not mutate membership.
            # The established approval gate still ensures that at most one
            # candidate can advance at a time, so preserve Action-1 behaviour
            # while surfacing the in-flight state to operators.
            return self._source_result(
                space_id=space_id,
                state="pairing_in_flight",
                hive_status=product_hive_status,
                projection=projection,
                source_ready=True,
                can_create_invitation=True,
            )
        return self._source_result(
            space_id=space_id,
            state="ready",
            hive_status="hivemind_healthy",
            projection=projection,
            source_ready=True,
            can_create_invitation=True,
        )

    async def list_source_eligibility(self) -> list[dict[str, Any]]:
        """Inspect each bounded top-level space without pairing-history scans."""

        storage = self._storage_factory()
        list_prefixes = getattr(storage, "list_prefixes", None)
        if list_prefixes is None:
            # A bounded object listing cannot distinguish one object-rich space
            # from too many spaces. Refuse instead of returning a partial view.
            raise MeshPairingServiceError(
                "mesh_status_inventory_unavailable",
                "Mesh status requires bounded top-level prefix inventory",
            )
        raw_prefix_limit = (
            _STATUS_MAX_SPACES + _STATUS_KNOWN_SYSTEM_PREFIXES + 1
        )
        try:
            prefixes = await list_prefixes("", max_prefixes=raw_prefix_limit)
        except TypeError as exc:
            raise MeshPairingServiceError(
                "mesh_status_inventory_unavailable",
                "Mesh status storage does not support bounded prefix inventory",
            ) from exc
        except Exception as exc:
            raise MeshPairingServiceError(
                "mesh_status_inventory_unavailable",
                "Mesh status top-level prefix inventory is unavailable",
            ) from exc
        space_ids = sorted(
            prefix.rstrip("/")
            for prefix in prefixes
            if isinstance(prefix, str)
            and not prefix.startswith("_")
            and _SOURCE_SPACE_ID_RE.fullmatch(prefix.rstrip("/"))
        )
        if len(prefixes) >= raw_prefix_limit and len(space_ids) <= _STATUS_MAX_SPACES:
            raise MeshPairingServiceError(
                "mesh_status_inventory_unavailable",
                "Mesh status top-level prefix inventory could not be proven complete",
            )
        if len(space_ids) > _STATUS_MAX_SPACES:
            raise MeshPairingServiceError(
                "mesh_status_inventory_too_large",
                "Mesh status space inventory exceeds its safety bound",
            )
        semaphore = asyncio.Semaphore(8)

        async def inspect(space_id: str) -> dict[str, Any]:
            async with semaphore:
                try:
                    return await self._inspect_source_eligibility(space_id)
                except Exception:
                    # Source readiness is a diagnostic fan-out.  One transient
                    # per-space backend failure must never hide unrelated
                    # pairings, recovery controls, or healthy neighbours.
                    return self._source_result(
                        space_id=space_id,
                        state="unavailable",
                        hive_status="unavailable",
                        projection=self._source_projection(
                            space_id=space_id,
                            observation="source_inspection_unavailable",
                        ),
                    )

        inspected = await asyncio.gather(*(inspect(space_id) for space_id in space_ids))
        return [item for item in inspected if item["state"] != "not_a_space"]

    async def _put_exact_genesis_model(
        self,
        *,
        key: str,
        expected: Any,
        getter: Callable[[], Any],
    ) -> None:
        """Add one missing model and require exact semantic readback."""

        current = await getter()
        if current is not None:
            if self._model_payload(current) == self._model_payload(expected):
                return
            raise MeshPairingServiceError(
                "source_prepare_conflict",
                "source preparation found divergent durable state",
            )
        try:
            await self._storage_factory().put_json(
                key, self._model_payload(expected)
            )
        except Exception as exc:
            raise MeshPairingServiceError(
                "source_prepare_interrupted",
                "source preparation write was interrupted; retry exact preparation",
            ) from exc
        observed = await getter()
        if observed is None or self._model_payload(observed) != self._model_payload(expected):
            raise MeshPairingServiceError(
                "source_prepare_conflict",
                "source preparation failed durable read-back validation",
            )

    def _assert_source_bootstrap_capacity(self, snapshot) -> None:
        """Apply the configured transfer bounds to an exact bootstrap snapshot."""

        try:
            serialize_snapshot(
                snapshot,
                max_objects=self._config.bootstrap_max_objects,
                max_bytes=self._config.bootstrap_max_bytes,
            )
        except MeshBootstrapError as exc:
            if exc.code in {"too_many_objects", "too_large"}:
                raise MeshPairingServiceError(
                    "bootstrap_limit_exceeded",
                    "source snapshot exceeds the configured bootstrap limits",
                ) from exc
            raise MeshPairingServiceError(
                "source_prepare_conflict",
                "source snapshot failed bounded bootstrap serialization",
            ) from exc

    async def _deep_source_bootstrap_preflight(self, space_id: str):
        """Prove the exact bounded export before an irreversible action."""

        try:
            snapshot = await self._bootstrap().export_snapshot(
                space_id,
                max_objects=self._config.bootstrap_max_objects,
                max_bytes=self._config.bootstrap_max_bytes,
            )
        except BootstrapLimitError as exc:
            raise MeshPairingServiceError(
                "bootstrap_limit_exceeded",
                "source snapshot exceeds the configured bootstrap limits",
            ) from exc
        except BootstrapError as exc:
            raise MeshPairingServiceError(
                "source_unhealthy",
                "source failed full bounded bootstrap validation",
            ) from exc
        self._assert_source_bootstrap_capacity(snapshot)
        return snapshot

    async def _assert_admission_bootstrap_capacity(
        self, space_id: str, snapshot, candidate: Member
    ) -> None:
        store = self._hive_store(space_id)
        node = await store.get_node_identity()
        term = await store.get_term()
        if node is None or term is None:
            raise MeshPairingServiceError(
                "source_unhealthy",
                "source authority changed before membership admission",
            )
        try:
            projected = self._bootstrap().project_membership_admission_snapshot(
                snapshot,
                space_id=space_id,
                candidate=candidate,
                source_node=node,
                term=term,
            )
        except BootstrapError as exc:
            raise MeshPairingServiceError(
                "source_unhealthy",
                "source admission snapshot could not be projected",
            ) from exc
        self._assert_source_bootstrap_capacity(projected)

    async def prepare_source(
        self,
        space_id: str,
        *,
        expected_state_token: str,
        quiesced: bool,
    ) -> dict[str, Any]:
        """Prepare a committed local space as a one-member Mesh source."""

        if quiesced is not True:
            raise MeshPairingServiceError(
                "quiescence_required",
                "writers-quiesced confirmation is required",
            )
        if (
            type(expected_state_token) is not str
            or re.fullmatch(r"[0-9a-f]{64}", expected_state_token, re.ASCII) is None
        ):
            raise MeshPairingServiceError(
                "invalid_state_token", "invalid source readiness token"
            )
        if type(space_id) is not str or _SOURCE_SPACE_ID_RE.fullmatch(space_id) is None:
            raise MeshPairingServiceError("not_a_space", "invalid source space id")

        locks = get_lock_manager()
        async with self.store.space_lock(space_id):
            async with locks.space_lifecycle(space_id):
                consolidation_lock = locks.consolidation(space_id)
                if consolidation_lock.locked():
                    raise MeshPairingServiceError(
                        "source_busy", "same-space maintenance is still running"
                    )
                async with consolidation_lock:
                    async with self._membership(space_id).space_lock():
                        return await self._prepare_source_locked(
                            space_id, expected_state_token=expected_state_token
                        )

    async def _prepare_source_locked(
        self, space_id: str, *, expected_state_token: str
    ) -> dict[str, Any]:
        """Run preparation while lifecycle/consolidation locks are held."""

        # Queue admission and intent creation are one ordered hand-off. An
        # enqueue that lands first is visible and refuses preparation. Once the
        # PREPARING record is durable we release admission: waiting enqueues then
        # observe that durable fence and refuse instead of slipping through
        # after the later COMPLETE provenance write.
        async with self._consolidation_queue.space_admission_lock(space_id):
            current = await self.inspect_source_eligibility(space_id)
            if current["state_token"] != expected_state_token:
                raise MeshPairingServiceError(
                    "source_state_changed",
                    "source state changed; refresh before preparing",
                )
            if current["state"] == "ready":
                return {"result": "already_ready", "source": current}
            if current["state"] not in {
                "local_only_can_prepare",
                "preparing",
            }:
                raise MeshPairingServiceError(
                    current["reason_code"], current["message"]
                )

            lane = await self._consolidation_queue.get_space_readiness_summary(
                space_id
            )
            if lane.get("running_job_id") or lane.get("queued_count", 0):
                raise MeshPairingServiceError(
                    "source_busy",
                    "same-space maintenance is still active or queued",
                )

            intent = await self.store.get_source_preparation(space_id)
            if intent is None:
                now_ms = self._clock_ms()
                started_at_iso = datetime.fromtimestamp(
                    now_ms / 1000, tz=timezone.utc
                ).isoformat(timespec="milliseconds")
                intent = SourcePreparationIntent(
                    preparation_id="prep_" + uuid.uuid4().hex,
                    protocol_version=1,
                    state=SourcePreparationState.PREPARING.value,
                    space_id=space_id,
                    source_fingerprint=self._config.fingerprint,
                    membership_public_key=_legacy_membership_key(
                        self._config.public_key
                    ),
                    node_id=_node_id_from_fingerprint(self._config.fingerprint),
                    display_name=self._config.display_name,
                    public_url=self._config.public_url,
                    started_at_ms=now_ms,
                    started_at_iso=started_at_iso,
                    completed_at_ms=0,
                    expected_state_token=expected_state_token,
                )
                expected = self._source_genesis_models(intent)
                try:
                    projected_snapshot = (
                        await self._bootstrap().project_source_preparation_snapshot(
                            space_id,
                            node=expected["node"],
                            membership=expected["membership"],
                            term=expected["term"],
                            token=expected["token"],
                            pointer=expected["pointer"],
                            max_objects=self._config.bootstrap_max_objects,
                            max_bytes=self._config.bootstrap_max_bytes,
                        )
                    )
                except BootstrapLimitError as exc:
                    raise MeshPairingServiceError(
                        "bootstrap_limit_exceeded",
                        "source snapshot exceeds the configured bootstrap limits",
                    ) from exc
                except BootstrapError as exc:
                    raise MeshPairingServiceError(
                        "source_prepare_preflight_failed",
                        "source snapshot could not be projected safely",
                    ) from exc
                except Exception as exc:
                    raise MeshPairingServiceError(
                        "source_prepare_preflight_failed",
                        "source snapshot could not be projected safely",
                    ) from exc
                self._assert_source_bootstrap_capacity(projected_snapshot)
                await self.store.put_source_preparation(intent)

        exact, phase, expected = await self._preparation_progress(space_id, intent)
        if not exact:
            raise MeshPairingServiceError(
                "prepare_recovery_required",
                "source preparation state diverged; automatic recovery is refused",
        )
        store = self._hive_store(space_id)

        if phase != "healthy":
            await self._put_exact_genesis_model(
                key=layout.node_status_key(space_id),
                expected=expected["unsafe"],
                getter=store.get_node_status,
            )
            await self._put_exact_genesis_model(
                key=layout.node_key(space_id),
                expected=expected["node"],
                getter=store.get_node_identity,
            )
            await self._put_exact_genesis_model(
                key=layout.members_key(space_id),
                expected=expected["membership"],
                getter=store.get_membership,
            )
            await self._put_exact_genesis_model(
                key=layout.term_key(space_id),
                expected=expected["term"],
                getter=store.get_term,
            )
            await self._put_exact_genesis_model(
                key=layout.token_key(space_id),
                expected=expected["token"],
                getter=store.get_token,
            )
            await self._put_exact_genesis_model(
                key=layout.bank_version_key(space_id),
                expected=expected["pointer"],
                getter=store.get_bank_version_pointer,
            )

            exact, phase, _ = await self._preparation_progress(space_id, intent)
            if not exact or phase != "pointer":
                raise MeshPairingServiceError(
                    "prepare_recovery_required",
                    "source preparation did not reach an exact baseline",
                )
            try:
                prepared_snapshot = await self._bootstrap().validate_source_preparation(
                    space_id,
                    initializing_reason=_SOURCE_INITIALIZATION_REASON,
                    max_objects=self._config.bootstrap_max_objects,
                    max_bytes=self._config.bootstrap_max_bytes,
                )
            except BootstrapLimitError as exc:
                raise MeshPairingServiceError(
                    "bootstrap_limit_exceeded",
                    "source snapshot exceeds the configured bootstrap limits",
                ) from exc
            except BootstrapError as exc:
                raise MeshPairingServiceError(
                    "prepare_recovery_required",
                    "source preparation failed bootstrap readiness validation",
                ) from exc
            self._assert_source_bootstrap_capacity(prepared_snapshot)

            # HEALTHY is the final shared protocol write. Only local operational
            # migration/provenance records are persisted afterward.
            current_health = await store.get_node_status()
            if current_health is None or self._model_payload(
                current_health
            ) != self._model_payload(expected["unsafe"]):
                raise MeshPairingServiceError(
                    "prepare_recovery_required",
                    "source preparation health marker diverged",
                )
            await store.set_node_status(expected["healthy"])
            observed_health = await store.get_node_status()
            if observed_health is None or self._model_payload(
                observed_health
            ) != self._model_payload(expected["healthy"]):
                raise MeshPairingServiceError(
                    "prepare_recovery_required",
                    "source preparation HEALTHY marker was not verified",
                )
        else:
            # Exact crash-after-HEALTHY retry: normal export must now validate
            # before the local provenance record completes.
            try:
                prepared_snapshot = await self._bootstrap().export_snapshot(
                    space_id,
                    max_objects=self._config.bootstrap_max_objects,
                    max_bytes=self._config.bootstrap_max_bytes,
                )
            except BootstrapLimitError as exc:
                raise MeshPairingServiceError(
                    "bootstrap_limit_exceeded",
                    "source snapshot exceeds the configured bootstrap limits",
                ) from exc
            except BootstrapError as exc:
                raise MeshPairingServiceError(
                    "prepare_recovery_required",
                    "prepared source no longer passes export validation",
                ) from exc
            self._assert_source_bootstrap_capacity(prepared_snapshot)

        # A newly converted source cannot have legacy source activations. Seed
        # the per-space migration sentinel now so its first approval never
        # depends on an append-only historical session scan.
        await self.store.put_activation_migration(
            space_id, "", now_ms=self._clock_ms()
        )
        await self.store.put_source_preparation(intent.complete(self._clock_ms()))
        source = await self.inspect_source_eligibility(space_id)
        if source["state"] != "ready":
            raise MeshPairingServiceError(
                "prepare_recovery_required",
                "prepared source did not become invitation-ready",
            )
        return {"result": "prepared", "source": source}

    # ==================================================================
    # SOURCE — Action 1: create invitation
    # ==================================================================

    async def create_invitation(self, space_id: str, *, requested_scopes: tuple[str, ...]) -> dict:
        """Issue a signed one-time invitation for ``space_id`` (source side)."""

        async with self.store.space_lock(space_id):
            async with self._membership(space_id).space_lock(), token_mutation_lock(
                space_id
            ):
                return await self._create_invitation_locked(
                    space_id, requested_scopes=requested_scopes
                )

    async def _create_invitation_locked(
        self, space_id: str, *, requested_scopes: tuple[str, ...]
    ) -> dict:
        """Create after the shared source-readiness predicate under its lock."""

        readiness = await self.inspect_source_eligibility(space_id)
        if readiness["can_create_invitation"] is not True:
            # Preserve the established Action-1 service codes for callers while
            # the richer admin readiness projection keeps its closed state
            # vocabulary. The predicate itself remains shared and authoritative.
            invitation_code = {
                "multi_member": "multi_member_source",
                "local_only_can_prepare": "not_meshable",
                "preparing": "not_meshable",
                "prepare_recovery_required": "not_meshable",
                "not_a_space": "not_meshable",
                "unsafe": "source_unhealthy",
                "resync_required": "source_unhealthy",
            }.get(readiness["state"], readiness["reason_code"])
            raise MeshPairingServiceError(
                invitation_code, readiness["message"]
            )

        # Readiness intentionally validates only the bounded authority head.
        # Invitation issuance is the action boundary: prove the full exact
        # export before generating a pair id/secret or persisting a session.
        await self._deep_source_bootstrap_preflight(space_id)

        store = self._hive_store(space_id)
        node = await store.get_node_identity()
        membership = await store.get_membership()
        if node is None or membership is None:  # readiness narrows this; fail closed
            raise MeshPairingServiceError(
                "unsafe", "source readiness changed before invitation creation"
            )

        pair_id = self._pair_id_factory()
        secret = self._secret_factory()
        secret_digest = hash_invitation_secret(secret, pair_id=pair_id, space_id=space_id)
        now = self._clock_ms()
        invitation = MeshInvitation(
            protocol_version=1,
            kind=MeshArtifactKind.INVITATION,
            pair_id=pair_id,
            space_id=space_id,
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            target_binding=MESH_TARGET_UNBOUND,
            membership_epoch=membership.epoch,
            issued_at_ms=now,
            expires_at_ms=now + MESH_INVITATION_TTL_MILLISECONDS,
            nonce=generate_pairing_nonce(),
            secret_digest=secret_digest,
        )
        signed = SignedMeshArtifact.sign(invitation, self._config.private_key)
        session = MeshPairingSession(
            pair_id=pair_id,
            role=MeshPairingRole.SOURCE.value,
            state=MeshPairingState.ISSUED.value,
            space_id=space_id,
            protocol_version=1,
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            source_endpoint=self._config.public_url,
            target_public_key="",
            target_fingerprint="",
            target_endpoint="",
            granted_scopes=tuple(sorted(set(requested_scopes) | {"read"})),
            base_epoch=membership.epoch,
            invitation_digest=signed.digest(),
            secret_digest=secret_digest,
            claim_digest="",
            approval_digest="",
            bootstrap_manifest_digest="",
            bootstrap_bank_version=-1,
            activation_event_id="",
            last_error="",
            created_at_ms=now,
            updated_at_ms=now,
            expires_at_ms=now + MESH_INVITATION_TTL_MILLISECONDS,
        )
        await self.store.put_blob(pair_id, "invitation", signed.canonical_bytes())
        await self.store.put_session(session)
        return {
            "pair_id": pair_id,
            "secret": secret,
            "invitation_bytes": signed.canonical_bytes(),
            "source_endpoint": self._config.public_url,
            "source_fingerprint": self._config.fingerprint,
        }

    # ==================================================================
    # TARGET — Action 2: accept invitation (reserve + claim)
    # ==================================================================

    async def accept_invitation(
        self,
        invitation_bytes: bytes,
        target_space_id: str,
        *,
        secret: str,
        source_endpoint: str,
        requested_scopes: tuple[str, ...],
        quiesced: bool,
    ) -> dict:
        """Verify an invitation, reserve the blank target, and send a join claim.

        The raw one-time ``secret`` (out-of-band, transport-only) proves possession
        to the source; ``source_endpoint`` is where the source peer is reached.
        """

        # The blank-target probe and subsequent raw reservation cannot form a
        # universal write transaction: ordinary writers may live in another
        # process or pass their own preflight before this method arms its
        # fence.  As for source preparation, accepting an invitation therefore
        # requires an explicit operator attestation that every same-space
        # writer and maintenance job has been quiesced.  This is intentionally
        # checked before parsing or persisting any invitation state.
        if quiesced is not True:
            raise MeshPairingServiceError(
                "quiescence_required",
                "writers-quiesced confirmation is required before accepting a target",
            )

        signed_invitation = SignedMeshArtifact.from_bytes(invitation_bytes)
        signed_invitation.verify()
        invitation = signed_invitation.artifact
        if type(invitation) is not MeshInvitation:
            raise MeshPairingServiceError("bad_invitation", "not a Mesh invitation")
        now = self._clock_ms()
        if now >= invitation.expires_at_ms:
            raise MeshPairingServiceError("expired", "invitation has expired")
        # Reject self-pairing (source == this instance).
        if decode_membership_public_key(invitation.source_public_key) == decode_membership_public_key(
            self._config.public_key
        ):
            raise MeshPairingServiceError("self_pairing", "cannot pair a space with itself")
        # The reserved space MUST be the space actually enrolled. Every downstream
        # fetch/import/finalize/release uses session.space_id (== invitation.space_id),
        # so a target_space_id that differs would reserve one space while enrolling
        # another — leaving the reserved space stranded and the enrolled one
        # unguarded. Refuse the mismatch before reserving anything.
        if target_space_id != invitation.space_id:
            raise MeshPairingServiceError(
                "space_mismatch", "target space must equal the invitation space"
            )

        # Endpoint input crosses a trust boundary: parse and canonicalize it
        # before the blank-target check, reservation, session/artifact writes,
        # or an injected in-process sender can observe any side effect.
        canonical_source_endpoint = self._canonical_peer_endpoint(source_endpoint)
        canonical_target_endpoint = self._canonical_peer_endpoint(
            self._config.public_url, peer="local"
        )

        pair_id = invitation.pair_id
        requested = tuple(sorted(set(requested_scopes) | {"read"}))
        canonical_invitation = signed_invitation.canonical_bytes()
        acceptance_intent = {
            "pair_id": pair_id,
            "space_id": target_space_id,
            "invitation_digest": signed_invitation.digest(),
            "source_fingerprint": invitation.source_fingerprint,
            "target_fingerprint": self._config.fingerprint,
            "requested_scopes": list(requested),
        }
        # Pair-first serialization prevents two target spaces (or two different
        # signed invitations) from racing on a caller-controlled pair id.  The
        # lock covers the identity decision, reservation and immutable local
        # artifacts; the outbound claim happens only after it is released.
        async with self.store.pair_lock(pair_id):
            (
                existing,
                retained_invitation,
                retained_claim,
                retained_intent,
            ) = await asyncio.gather(
                self.store.get_session(pair_id),
                self.store.get_blob(pair_id, "invitation"),
                self.store.get_blob(pair_id, "claim"),
                self.store.get_target_acceptance_intent(pair_id),
            )
            if retained_intent is not None and retained_intent != acceptance_intent:
                raise MeshPairingServiceError(
                    "pair_conflict", "pairing id is already bound to another target acceptance"
                )
            # Before #417, a crash could leave only ``reserve(space, pair)``.
            # That record carries no invitation identity, so it cannot be
            # safely adopted from caller input: a different signed invitation
            # with the same pair id could otherwise hijack the reservation.
            # New prefixes always have the immutable intent first; legacy bare
            # reservations require the explicit audited recovery method below.
            reserved_spaces = await self.store.find_reservations_by_pair_id(pair_id)
            if (
                existing is None
                and retained_invitation is None
                and retained_claim is None
                and reserved_spaces
                and reserved_spaces != (target_space_id,)
            ):
                # Even a syntactically valid acceptance intent is only a local
                # prefix record.  Storage damage must not rewrite I(X) -> I(Y)
                # and use the same caller-controlled pair id to reserve Y while
                # X is still fenced.  A fresh/new-prefix retry may see no
                # reservation yet, or exactly its own target reservation; every
                # other observed reservation set is a fail-closed conflict.
                raise MeshPairingServiceError(
                    "pair_conflict",
                    "pairing id is already reserved by another target space",
                )
            if (
                existing is None
                and retained_invitation is None
                and retained_claim is None
                and retained_intent is None
                and reserved_spaces
            ):
                raise MeshPairingServiceError(
                    "pair_conflict",
                    "pairing id has an unbound legacy reservation; operator recovery is required",
                )
            if existing is None and (
                retained_invitation is not None
                or retained_claim is not None
                # Crash prefix: new #417 writes intent/fence/raw reservation
                # before the first local artifact.  The exact same signed
                # invitation may complete that prefix, but an intent for any
                # other target/input is already rejected above and a bare raw
                # reservation remains operator-only legacy recovery.
                or (
                    retained_intent == acceptance_intent
                    and reserved_spaces == (target_space_id,)
                )
            ):
                # Recover only the exact write prefixes produced below
                # (intent/fence/reserve -> invitation -> claim -> session). A
                # partial target acceptance is not free-form state: every
                # retained artifact must validate against this signed invitation
                # and target identity, and the artifact-less form must retain
                # the exact immutable intent plus raw owner.
                if retained_claim is not None and retained_invitation is None:
                    raise MeshPairingServiceError(
                        "pair_conflict", "pairing id has an impossible local prefix"
                    )
                if retained_invitation is not None:
                    try:
                        stored_invitation = SignedMeshArtifact.from_bytes(
                            retained_invitation
                        )
                        stored_invitation.verify()
                    except Exception as exc:
                        raise MeshPairingServiceError(
                            "pair_conflict", "pairing id has invalid local invitation"
                        ) from exc
                    if (
                        type(stored_invitation.artifact) is not MeshInvitation
                        or stored_invitation.canonical_bytes()
                        != canonical_invitation
                    ):
                        raise MeshPairingServiceError(
                            "pair_conflict", "pairing id is already bound to another invitation"
                        )
                if retained_claim is not None:
                    try:
                        signed_claim = SignedMeshArtifact.from_bytes(retained_claim)
                        signed_claim.verify()
                        claim = signed_claim.artifact
                    except Exception as exc:
                        raise MeshPairingServiceError(
                            "pair_conflict", "pairing id has invalid local claim"
                        ) from exc
                    if (
                        not isinstance(claim, MeshJoinClaim)
                        or claim.pair_id != pair_id
                        or claim.space_id != invitation.space_id
                        or claim.source_public_key != invitation.source_public_key
                        or claim.source_fingerprint != invitation.source_fingerprint
                        or claim.target_public_key != self._config.public_key
                        or claim.target_fingerprint != self._config.fingerprint
                        or claim.membership_epoch != invitation.membership_epoch
                        or claim.invitation_digest != signed_invitation.digest()
                        or claim.requested_scopes != requested
                    ):
                        raise MeshPairingServiceError(
                            "pair_conflict", "pairing id has a conflicting local claim"
                        )
                else:
                    claim = MeshJoinClaim(
                        protocol_version=1,
                        kind=MeshArtifactKind.JOIN_CLAIM,
                        pair_id=pair_id,
                        space_id=invitation.space_id,
                        source_public_key=invitation.source_public_key,
                        source_fingerprint=invitation.source_fingerprint,
                        target_public_key=self._config.public_key,
                        target_fingerprint=self._config.fingerprint,
                        membership_epoch=invitation.membership_epoch,
                        issued_at_ms=now,
                        nonce=generate_pairing_nonce(),
                        invitation_digest=signed_invitation.digest(),
                        requested_scopes=requested,
                    )
                    signed_claim = SignedMeshArtifact.sign(claim, self._config.private_key)
                session = MeshPairingSession(
                    pair_id=pair_id,
                    role=MeshPairingRole.TARGET.value,
                    state=MeshPairingState.CLAIMED.value,
                    space_id=invitation.space_id,
                    protocol_version=1,
                    source_public_key=invitation.source_public_key,
                    source_fingerprint=invitation.source_fingerprint,
                    source_endpoint=canonical_source_endpoint,
                    target_public_key=self._config.public_key,
                    target_fingerprint=self._config.fingerprint,
                    target_endpoint=canonical_target_endpoint,
                    granted_scopes=requested,
                    base_epoch=invitation.membership_epoch,
                    invitation_digest=signed_invitation.digest(),
                    secret_digest="",
                    claim_digest=signed_claim.digest(),
                    approval_digest="",
                    bootstrap_manifest_digest="",
                    bootstrap_bank_version=-1,
                    activation_event_id="",
                    last_error="",
                    created_at_ms=now,
                    updated_at_ms=now,
                    expires_at_ms=invitation.issued_at_ms + MESH_INVITATION_TTL_MILLISECONDS,
                )
                async with self.store.space_lock(target_space_id):
                    # Legacy partial prefixes did not carry the pre-reservation
                    # binding.  Their signed artifacts and exact reservation
                    # above make this one-time completion safe; all new prefixes
                    # write the intent before reserve.
                    # An incomplete prefix must already own the exact durable
                    # reservation before it can touch a released direct tail.
                    # Checking only after _arm could replace P0's released
                    # fence with P1, then discover that raw P0 is still held
                    # after a crash before its delete.
                    if (
                        await self.store.get_reservation_direct(target_space_id)
                        != pair_id
                    ):
                        raise MeshPairingServiceError(
                            "pair_conflict", "partial pairing reservation is not owned"
                        )
                    await self.store.put_target_acceptance_intent(
                        pair_id, acceptance_intent
                    )
                    await self._arm_target_pairing_fence(session)
                    if retained_invitation is None:
                        await self.store.put_blob(
                            pair_id, "invitation", canonical_invitation
                        )
                    if retained_claim is None:
                        await self.store.put_blob(
                            pair_id, "claim", signed_claim.canonical_bytes()
                        )
                    await self.store.put_session(session)
            elif existing is not None or retained_invitation is not None or retained_claim is not None:
                if existing is None or retained_invitation is None or retained_claim is None:
                    raise MeshPairingServiceError(
                        "pair_conflict", "pairing id already has incomplete local state"
                    )
                try:
                    stored_invitation = SignedMeshArtifact.from_bytes(retained_invitation)
                    stored_claim = SignedMeshArtifact.from_bytes(retained_claim)
                    stored_invitation.verify()
                    stored_claim.verify()
                    claim = stored_claim.artifact
                except Exception as exc:
                    raise MeshPairingServiceError(
                        "pair_conflict", "pairing id has invalid local artifacts"
                    ) from exc
                try:
                    stored_source_endpoint = self._canonical_peer_endpoint(
                        existing.source_endpoint
                    )
                    stored_target_endpoint = self._canonical_peer_endpoint(
                        existing.target_endpoint, peer="target"
                    )
                except MeshPairingServiceError as exc:
                    raise MeshPairingServiceError(
                        "pair_conflict", "pairing has an invalid persisted endpoint"
                    ) from exc
                if (
                    type(stored_invitation.artifact) is not MeshInvitation
                    or stored_invitation.canonical_bytes() != canonical_invitation
                    or not isinstance(claim, MeshJoinClaim)
                    or claim.pair_id != pair_id
                    or claim.space_id != invitation.space_id
                    or claim.source_public_key != invitation.source_public_key
                    or claim.source_fingerprint != invitation.source_fingerprint
                    or claim.target_public_key != self._config.public_key
                    or claim.target_fingerprint != self._config.fingerprint
                    or claim.membership_epoch != invitation.membership_epoch
                    or claim.invitation_digest != signed_invitation.digest()
                    or claim.requested_scopes != requested
                    or existing.role != MeshPairingRole.TARGET.value
                    or existing.space_id != invitation.space_id
                    or existing.source_public_key != invitation.source_public_key
                    or existing.source_fingerprint != invitation.source_fingerprint
                    or stored_source_endpoint != canonical_source_endpoint
                    or existing.target_public_key != self._config.public_key
                    or existing.target_fingerprint != self._config.fingerprint
                    or stored_target_endpoint != canonical_target_endpoint
                    or existing.base_epoch != invitation.membership_epoch
                    or existing.invitation_digest != signed_invitation.digest()
                    or existing.claim_digest != stored_claim.digest()
                    or existing.granted_scopes != requested
                ):
                    raise MeshPairingServiceError(
                        "pair_conflict", "pairing id is already bound to another invitation"
                    )
                # Canonicalize compatible pre-#417 HTTPS endpoints as an
                # idempotent operational migration.  Invalid/unsafe legacy
                # endpoints remain a fail-closed conflict and require the
                # explicit endpoint-repair path; they are never reused raw.
                if (
                    existing.source_endpoint != stored_source_endpoint
                    or existing.target_endpoint != stored_target_endpoint
                ):
                    existing = existing.with_fields(
                        now_ms=self._clock_ms(),
                        source_endpoint=stored_source_endpoint,
                        target_endpoint=stored_target_endpoint,
                    )
                    await self.store.put_session(existing)
                async with self.store.space_lock(target_space_id):
                    reservation = await self.store.get_reservation(target_space_id)
                    if not existing.is_terminal():
                        if reservation != pair_id:
                            raise MeshPairingServiceError(
                                "pair_conflict",
                                "pairing reservation is no longer owned",
                            )
                        # The intent is a pre-reservation provenance record,
                        # not an upgrade marker for old terminal history.  A
                        # compatible retry may add it only while the current
                        # non-terminal target session still owns its fence;
                        # otherwise a legacy ACTIVE session would be silently
                        # reclassified as a #417 receipt-loss tail.
                        await self.store.put_target_acceptance_intent(
                            pair_id, acceptance_intent
                        )
                        await self._arm_target_pairing_fence(existing)
                session = existing
                signed_claim = stored_claim
            else:
                # A raw invitation secret must accompany the invitation bytes out
                # of band; it is deliberately not persisted with the local claim.
                claim = MeshJoinClaim(
                    protocol_version=1,
                    kind=MeshArtifactKind.JOIN_CLAIM,
                    pair_id=pair_id,
                    space_id=invitation.space_id,
                    source_public_key=invitation.source_public_key,
                    source_fingerprint=invitation.source_fingerprint,
                    target_public_key=self._config.public_key,
                    target_fingerprint=self._config.fingerprint,
                    membership_epoch=invitation.membership_epoch,
                    issued_at_ms=now,
                    nonce=generate_pairing_nonce(),
                    invitation_digest=signed_invitation.digest(),
                    requested_scopes=requested,
                )
                signed_claim = SignedMeshArtifact.sign(claim, self._config.private_key)
                session = MeshPairingSession(
                    pair_id=pair_id,
                    role=MeshPairingRole.TARGET.value,
                    state=MeshPairingState.CLAIMED.value,
                    space_id=invitation.space_id,
                    protocol_version=1,
                    source_public_key=invitation.source_public_key,
                    source_fingerprint=invitation.source_fingerprint,
                    source_endpoint=canonical_source_endpoint,
                    target_public_key=self._config.public_key,
                    target_fingerprint=self._config.fingerprint,
                    target_endpoint=canonical_target_endpoint,
                    granted_scopes=requested,
                    base_epoch=invitation.membership_epoch,
                    invitation_digest=signed_invitation.digest(),
                    secret_digest="",
                    claim_digest=signed_claim.digest(),
                    approval_digest="",
                    bootstrap_manifest_digest="",
                    bootstrap_bank_version=-1,
                    activation_event_id="",
                    last_error="",
                    created_at_ms=now,
                    updated_at_ms=now,
                    expires_at_ms=invitation.issued_at_ms + MESH_INVITATION_TTL_MILLISECONDS,
                )
                # Reserve the blank target under the nested space lock, proving
                # virginity first (V1 never merges a populated space into a cluster).
                async with self.store.space_lock(target_space_id):
                    # This preflight must precede intent/fence publication:
                    # a previous pair can have reached signed released direct
                    # authorities and then crashed before deleting its raw
                    # reservation.  A new pair may not replace those records
                    # and only then learn the raw space is still P0-owned.
                    if await self.store.get_reservation_direct(target_space_id) is not None:
                        raise MeshPairingServiceError(
                            "space_reserved", "target space is already reserved"
                        )
                    try:
                        await self._bootstrap()._assert_blank_target(target_space_id)
                    except BootstrapError as exc:
                        raise MeshPairingServiceError(
                            "populated_target", "target space is not blank"
                        ) from exc
                    # Persist before reservation.  A crash immediately after
                    # reserve then still leaves a durable pair-id -> space
                    # authority, so an attacker cannot reuse the caller-set
                    # pair id to reserve a second target space.
                    await self.store.put_target_acceptance_intent(
                        pair_id, acceptance_intent
                    )
                    await self._arm_target_pairing_fence(
                        session, replace_settled=True
                    )
                    await self.store.reserve(target_space_id, pair_id, now_ms=now)
                    await self.store.put_blob(pair_id, "invitation", canonical_invitation)
                    await self.store.put_blob(
                        pair_id, "claim", signed_claim.canonical_bytes()
                    )
                    await self.store.put_session(session)

        # Send the signed claim + the transport-only secret to the source peer.
        claim_body = canonical_dumps(
            {
                "claim": canonical_loads(signed_claim.canonical_bytes()),
                "secret": secret,
                "target_endpoint": canonical_target_endpoint,
            }
        )
        # A terminal/local-progress retry is already represented durably; it must
        # not manufacture a second claim nonce or overwrite signed artifacts.
        if session.state != MeshPairingState.CLAIMED.value:
            return {
                "pair_id": pair_id,
                "space_id": invitation.space_id,
                "membership_epoch": invitation.membership_epoch,
                "source_fingerprint": invitation.source_fingerprint,
                "state": session.state,
            }
        response = await self._client(canonical_source_endpoint).claim(
            space_id=invitation.space_id,
            epoch=invitation.membership_epoch,
            target_fingerprint=invitation.source_fingerprint,
            pair_id=pair_id,
            body=claim_body,
        )
        if response.status_code != 200:
            raise MeshPairingServiceError("claim_rejected", "source rejected the join claim")
        return {
            "pair_id": pair_id,
            "space_id": invitation.space_id,
            "membership_epoch": invitation.membership_epoch,
            "source_fingerprint": invitation.source_fingerprint,
            "state": MeshPairingState.CLAIMED.value,
        }

    async def set_target_source_endpoint(self, pair_id: str, source_endpoint: str) -> None:
        canonical_source_endpoint = self._canonical_peer_endpoint(source_endpoint)
        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.TARGET.value:
                raise MeshPairingServiceError("unknown_pair", "unknown target pairing")
            await self.store.put_session(
                session.with_fields(
                    now_ms=self._clock_ms(), source_endpoint=canonical_source_endpoint
                )
            )

    async def recover_orphaned_target_reservation(
        self, pair_id: str, *, space_id: str, operator: str
    ) -> dict:
        """Release an interrupted target-acceptance reservation under operator control.

        A pre-#417 reserve-only prefix has no identity at all.  A #417 process
        can also crash after its immutable intent and reservation write but
        before its first artifact/session write.  Neither shape can safely be
        continued after an expired invitation, but both are recoverable when
        this exact target space is still blank.  A retained join claim or any
        session makes the local prefix ambiguous: the claim could have reached
        the source before an individual target-session loss.  In that shape
        local storage cannot prove that freeing this target is safe, so it
        deliberately remains fenced for explicit cross-peer/operator recovery.
        """

        if not operator:
            raise MeshPairingServiceError(
                "operator_required", "orphaned reservation recovery requires an operator"
            )
        if _PAIR_ID_RE.fullmatch(pair_id) is None or _SOURCE_SPACE_ID_RE.fullmatch(
            space_id
        ) is None:
            raise MeshPairingServiceError(
                "invalid_pair", "invalid orphaned reservation identity"
            )
        async with self.store.pair_lock(pair_id):
            async with self.store.space_lock(space_id):
                (
                    session,
                    invitation,
                    claim,
                    intent,
                    reservation,
                    target_current_tail,
                    target_fence,
                    target_floor,
                ) = await asyncio.gather(
                    self.store.get_session(pair_id),
                    self.store.get_blob(pair_id, "invitation"),
                    self.store.get_blob(pair_id, "claim"),
                    self.store.get_target_acceptance_intent(pair_id),
                    self.store.get_reservation(space_id),
                    self.store.get_target_pairing_current_tail(space_id),
                    self.store.get_target_pairing_fence(space_id),
                    self.store.get_target_pairing_protocol_floor(space_id),
                )
                def _held_target_owner(
                    candidate: SignedTargetPairingFenceAuthority | None,
                ) -> bool:
                    if candidate is None:
                        return False
                    try:
                        candidate.verify(self._config.public_key)
                    except Exception as exc:
                        raise MeshPairingServiceError(
                            "not_orphaned",
                            "target pairing fence signature is invalid",
                        ) from exc
                    authority = candidate.authority
                    return (
                        authority.pair_id == pair_id
                        and authority.space_id == space_id
                        and authority.phase == "held"
                        and authority.target_fingerprint == self._config.fingerprint
                        and authority.target_public_key == self._config.public_key
                    )

                held_fence = _held_target_owner(target_fence)
                held_current_tail = _held_target_owner(target_current_tail)
                floor_only_prefix = (
                    not (held_fence or held_current_tail)
                    and _held_target_owner(target_floor)
                )
                if reservation != pair_id and not (
                    reservation is None
                    and (held_fence or held_current_tail or floor_only_prefix)
                ):
                    raise MeshPairingServiceError(
                        "not_orphaned", "reservation is not owned by this pairing"
                    )
                artifact_spaces: set[str] = set()
                for raw, label in ((invitation, "invitation"), (claim, "claim")):
                    if raw is None:
                        continue
                    try:
                        signed = SignedMeshArtifact.from_bytes(raw)
                        signed.verify()
                        artifact = signed.artifact
                    except Exception as exc:
                        raise MeshPairingServiceError(
                            "not_orphaned",
                            "reservation has unreadable pairing artifacts",
                        ) from exc
                    if (
                        type(artifact)
                        not in (MeshInvitation, MeshJoinClaim, MeshEnrollmentApproval)
                        or artifact.pair_id != pair_id
                    ):
                        raise MeshPairingServiceError(
                            "not_orphaned",
                            f"reservation has conflicting {label} artifact",
                        )
                    artifact_spaces.add(artifact.space_id)
                intent_space = intent["space_id"] if intent is not None else None
                # The target contacts the source only after it has durably
                # stored both the signed claim and its target session.  A
                # missing session is consequently *not* proof that the claim
                # never left this target: a single-record loss can erase that
                # session after a real source admission.  The explicit orphan
                # action may therefore free only a prefix with no retained
                # claim artifact.  An invitation-only prefix is still safe:
                # the local write order cannot have reached the outbound
                # request without first retaining the claim.  This deliberately
                # gives up convenience for a claim-only crash prefix in favour
                # of preserving the source's possible PENDING membership.
                safe_local_prefix = (
                    session is None
                    and claim is None
                    and (intent_space is None or intent_space == space_id)
                    and (not artifact_spaces or artifact_spaces == {space_id})
                )
                if not safe_local_prefix:
                    raise MeshPairingServiceError(
                        "not_orphaned", "reservation is bound to pairing state and cannot be released"
                    )
                try:
                    await self._bootstrap()._assert_blank_target(space_id)
                except BootstrapError as exc:
                    raise MeshPairingServiceError(
                        "populated_target", "orphaned reservation target is no longer blank"
                    ) from exc
                if held_fence or held_current_tail or floor_only_prefix:
                    await self._release_target_pairing_fence_for_orphan(
                        space_id=space_id, pair_id=pair_id
                    )
                if reservation == pair_id:
                    await self.store.release(space_id, pair_id)
        return {
            "pair_id": pair_id,
            "space_id": space_id,
            "state": "orphaned_reservation_released",
        }

    async def run_target_enrollment(self, pair_id: str) -> dict:
        """Drive the target through status -> bootstrap import -> final ACK, then
        await the source-delivered e+2 activation (target side, Action 3 part 2).

        Serialized under the target ``pair_lock`` and re-entrant by state, so the
        admin control plane can safely (re)invoke it. Holding the lock across the
        ACK cannot deadlock: the inbound self-activation the ACK triggers takes
        only the store's global lock, never this pair lock (source and target are
        always distinct instances). The final ACK triggers the source's Transition
        2 + activation delivery back to this instance, which self-activates via the
        confined router branch, so on return the session is ``active``.
        """

        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.TARGET.value:
                raise MeshPairingServiceError("unknown_pair", "unknown target pairing")
            state = session.state
            if state == MeshPairingState.ACTIVE.value:
                # An ACTIVE workflow record is not by itself authority to clear
                # the target's write fence.  This local-only retry is important
                # after a crash between the terminal receipt and reservation
                # release, when the source may be unavailable; re-prove the
                # retained import authority and exact e+2 membership first.
                await self._restore_target_activation_receipt_from_source_receipt(
                    session,
                    base=session.base_epoch,
                    require_terminal_confirmation=True,
                )
                if not await self._target_finalized_activation_matches(
                    session, base=session.base_epoch
                ):
                    reservation = await self.store.get_reservation(session.space_id)
                    if not (
                        reservation == pair_id
                        and await self._target_activation_receipt_matches(
                            session, base=session.base_epoch
                        )
                    ):
                        fenced = (
                            await self._fence_active_target_terminal_chain_loss(
                                session, base=session.base_epoch
                            )
                            if reservation is None
                            else await self._fence_target_activation_authority_loss(
                                session, base=session.base_epoch
                            )
                        )
                        if fenced:
                            return await self._final_ack_and_activate(pair_id, session)
                        raise MeshPairingServiceError(
                            "active_receipt_invalid",
                            "active target terminal proof is unavailable",
                        )
                completed = await self._prove_and_complete_active_target_tail(
                    session, base=session.base_epoch
                )
                if completed.code is not MeshResponseCode.OK:
                    if await self._fence_target_activation_authority_loss(
                        session, base=session.base_epoch
                    ):
                        return await self._final_ack_and_activate(pair_id, session)
                    raise MeshPairingServiceError(
                        "active_receipt_invalid",
                        "active target receipt lacks local e+2 authority",
                    )
                return {
                    "pair_id": pair_id,
                    "state": state,
                    "ack_status": 202,
                    "source_confirmation_pending": True,
                }
            # A signed terminal receipt is written only after the target has
            # proved and applied e+2, but before its HEALTHY/session/release
            # tail.  Complete that *local* tail without contacting the source
            # when a crash later loses the import marker.  Do not report a
            # final ACK success here: the source still needs a retry/resume to
            # observe the target's confirmation.
            if state in (
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
                MeshPairingState.TRANSFERRING.value,
            ) and await self._target_activation_receipt_matches(
                session, base=session.base_epoch
            ):
                completed = await self._finalize_target_activation(
                    session, base=session.base_epoch
                )
                if completed.code is MeshResponseCode.OK:
                    return {
                        "pair_id": pair_id,
                        "state": MeshPairingState.ACTIVE.value,
                        "ack_status": 202,
                    }
            if state == MeshPairingState.CLAIMED.value:
                signed_env = await self._fetch_and_verify_approval(session)
                # The source has already committed Transition 1 (admitted us PENDING
                # at e+1) before signing this approval, so from here WE are committed
                # (post-mutation): move to TRANSFERRING BEFORE importing, so any
                # import failure is blocked_recovery (resync reachable), never a
                # pre-mutation cancel that would strand the source's pending member.
                transferring = session.transition(
                    MeshPairingState.APPROVED, now_ms=self._clock_ms()
                ).transition(
                    MeshPairingState.TRANSFERRING,
                    now_ms=self._clock_ms(),
                    bootstrap_manifest_digest=signed_env.envelope.manifest_digest,
                    bootstrap_bank_version=signed_env.envelope.bank_version,
                )
                await self.store.put_session(transferring)
                session = await self._import_and_await(transferring, signed_env)
            elif state == MeshPairingState.AWAITING_ACKS.value:
                # Re-entrant retry after a transient ACK/activation failure: the
                # bootstrap import already succeeded; just (re)send the final ACK.
                pass
            elif state in (
                MeshPairingState.TRANSFERRING.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
            ):
                # Import is incomplete/failed (possibly a hard crash mid-import
                # that left no evidence): the blank-target teardown resync is
                # required before a fresh import — never re-import into a
                # non-blank space, and never strand this as a dead end.
                raise MeshPairingServiceError(
                    "resync_required", "target enrollment is incomplete; run resync"
                )
            else:
                raise MeshPairingServiceError(
                    "not_enrollable", "pairing is not in an enrollable state"
                )
            return await self._final_ack_and_activate(pair_id, session)

    async def resync(self, pair_id: str) -> dict:
        """Recover a target stranded by a corrupt/partial bootstrap import: tear the
        space back to blank, re-import a fresh snapshot from the (still
        ``transferring``) source, and re-drive to ``active`` — WITHOUT a source-side
        eviction of the already-admitted pending member (PROJECT_MESH.md §7).

        Evidence-gated: a hard crash mid-import can leave the session durably
        ``transferring`` with NO evidence, so that state is first converted to
        ``blocked_recovery`` with freshly signed evidence (a legal target edge) —
        eliminating the dead end — before the verified teardown proceeds.
        """

        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.TARGET.value:
                raise MeshPairingServiceError("unknown_pair", "unknown target pairing")
            # A verified terminal receipt is enough to finish the local target
            # tail and retry the source confirmation.  This must happen before
            # status/bootstrap preflight: a source may be offline precisely
            # because it crashed after e+2 while the target has the only safe
            # proof of its already-applied promotion.
            if session.state == MeshPairingState.ACTIVE.value:
                await self._restore_target_activation_receipt_from_source_receipt(
                    session,
                    base=session.base_epoch,
                    require_terminal_confirmation=True,
                )
                if not await self._target_finalized_activation_matches(
                    session, base=session.base_epoch
                ):
                    reservation = await self.store.get_reservation(session.space_id)
                    if not (
                        reservation == pair_id
                        and await self._target_activation_receipt_matches(
                            session, base=session.base_epoch
                        )
                    ):
                        fenced = (
                            await self._fence_active_target_terminal_chain_loss(
                                session, base=session.base_epoch
                            )
                            if reservation is None
                            else await self._fence_target_activation_authority_loss(
                                session, base=session.base_epoch
                            )
                        )
                        if fenced:
                            return await self._final_ack_and_activate(pair_id, session)
                        raise MeshPairingServiceError(
                            "not_resyncable",
                            "active target terminal proof is unavailable",
                        )
                completed = await self._prove_and_complete_active_target_tail(
                    session, base=session.base_epoch
                )
                if completed.code is not MeshResponseCode.OK:
                    if await self._fence_target_activation_authority_loss(
                        session, base=session.base_epoch
                    ):
                        return await self._final_ack_and_activate(pair_id, session)
                    raise MeshPairingServiceError(
                        "not_resyncable", "active target receipt lacks local e+2 authority"
                    )
                return await self._final_ack_and_activate(pair_id, session)
            if session.state in (
                MeshPairingState.TRANSFERRING.value,
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
            ) and await self._target_activation_receipt_matches(
                session, base=session.base_epoch
            ):
                completed = await self._finalize_target_activation(
                    session, base=session.base_epoch
                )
                if completed.code is MeshResponseCode.OK:
                    fresh = await self.store.get_session(pair_id)
                    return await self._final_ack_and_activate(
                        pair_id, fresh if fresh is not None else session
                    )
            # Prove that the source is presently able to serve its signed
            # snapshot BEFORE changing this target.  The source status/bootstrap
            # handlers refuse an UNSAFE/resyncing source, so a stale e+1 cannot
            # cause us to tear down a recoverable target space.
            signed_env = await self._fetch_and_verify_approval(
                session, require_current_source=True
            )
            # Fetch and verify the exact signed payload while the source is
            # still demonstrably bootstrap-ready, *before* destroying any
            # target state.  A source that flips UNSAFE in this interval now
            # refuses here with zero target mutation instead of stranding an
            # already-torn-down target in blocked recovery.
            boot_payload = await self._fetch_verified_bootstrap_payload(
                session, signed_env=signed_env
            )
            # A local e+2 delivery deliberately does not take this pair lock:
            # the source is still waiting for the target confirmation.  Lock
            # the destructive resync prefix with that delivery's finalization
            # tail, then re-read the receipt.  Otherwise a stale resync worker
            # can erase e+2 after the target has already made the source ACTIVE.
            active_receipt: MeshPairingSession | None = None
            terminal_receipt: MeshPairingSession | None = None
            async with self.store.space_lock(session.space_id):
                fresh = await self.store.get_session(pair_id)
                if fresh is None or fresh.role != MeshPairingRole.TARGET.value:
                    raise MeshPairingServiceError(
                        "resync_failed", "target pairing state disappeared during resync"
                    )
                if fresh.state == MeshPairingState.ACTIVE.value:
                    active_receipt = fresh
                elif await self._target_activation_receipt_matches(
                    fresh, base=fresh.base_epoch
                ):
                    # A source e+2 can race a preflighted resync.  The signed
                    # terminal receipt is stronger than the stale recovery
                    # intent, so never erase the already-applied target view.
                    terminal_receipt = fresh
                else:
                    if (
                        fresh.space_id != session.space_id
                        or fresh.base_epoch != session.base_epoch
                        or fresh.source_fingerprint != session.source_fingerprint
                        or fresh.target_fingerprint != session.target_fingerprint
                    ):
                        raise MeshPairingServiceError(
                            "resync_state_changed", "target pairing changed during resync"
                        )
                    session = fresh
                    if session.state == MeshPairingState.TRANSFERRING.value:
                        session = await self._block_recovery(
                            session,
                            phase="bootstrap_import_failed",
                            next_action="resync",
                            manifest_digest=session.bootstrap_manifest_digest,
                        )
                    elif session.state == MeshPairingState.AWAITING_ACKS.value and not await self._import_validation_matches(
                        session, base=session.base_epoch
                    ):
                        # A marker can be deleted/corrupted while the target is still
                        # UNSAFE.  Convert that precise proof loss into the same durable,
                        # evidence-gated repair route rather than stranding e+2 source
                        # membership behind an unrecoverable target reservation.
                        session = await self._block_recovery(
                            session,
                            phase="import_validation_failed",
                            next_action="resync",
                            manifest_digest=session.bootstrap_manifest_digest,
                        )
                    if session.state != MeshPairingState.BLOCKED_RECOVERY.value:
                        raise MeshPairingServiceError("not_resyncable", "pairing is not resyncable")
                    signed_ev = await self._verified_blocked_evidence(session)
                    if (
                        signed_ev.evidence.next_action != "resync"
                        or signed_ev.evidence.phase
                        not in {"bootstrap_import_failed", "import_validation_failed"}
                    ):
                        # A resume-class block (e.g. activation_unconfirmed) is the SOURCE's
                        # to recover; a target never tears its space down for those.
                        raise MeshPairingServiceError("not_resyncable", "blocked recovery is not a resync")
                    # Mark UNSAFE BEFORE any replacement write, then tear the space down to
                    # blank (UNSAFE marker deleted LAST) and import the preflight-verified
                    # signed snapshot.  No source network fetch remains after teardown.
                    store = self._hive_store(session.space_id)
                    await store.set_node_status(
                        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="mesh_resync")
                    )
                    try:
                        await self._teardown_target_space(session.space_id)
                        # A malformed/conflicting marker must not prevent the fresh,
                        # signed import from installing its replacement proof.  This
                        # narrow clear is legal only after the evidence-gated UNSAFE
                        # teardown above, and its storage readback is fail-closed.  It
                        # covers both an explicit marker mismatch and an import crash
                        # that discovered the conflict before it could classify it.
                        await self.store.clear_import_validation_for_resync(pair_id)
                        await self.store.clear_target_activation_receipt_for_resync(
                            pair_id
                        )
                    except MeshPairingServiceError:
                        raise
                    except Exception as exc:  # pragma: no cover - defensive safe wrap
                        raise MeshPairingServiceError(
                            "resync_failed", "target resync could not prepare a clean import"
                        ) from exc
                    transferring = session.transition(
                        MeshPairingState.TRANSFERRING,
                        now_ms=self._clock_ms(),
                        bootstrap_manifest_digest=signed_env.envelope.manifest_digest,
                        bootstrap_bank_version=signed_env.envelope.bank_version,
                    )
                    await self.store.put_session(transferring)
            if active_receipt is not None:
                # A terminal session is local workflow bookkeeping, not the
                # membership authority.  It may be syntactically rewritten by
                # damaged storage, so only repair an ACTIVE tail after the
                # retained signed e+1 authority and the exact local e+2 view
                # prove that this target was genuinely promoted.
                completed = await self._prove_and_complete_active_target_tail(
                    active_receipt, base=active_receipt.base_epoch
                )
                if completed.code is not MeshResponseCode.OK:
                    raise MeshPairingServiceError(
                        "not_resyncable", "active target receipt lacks local e+2 authority"
                    )
                return await self._final_ack_and_activate(pair_id, active_receipt)
            if terminal_receipt is not None:
                completed = await self._finalize_target_activation(
                    terminal_receipt, base=terminal_receipt.base_epoch
                )
                if completed.code is not MeshResponseCode.OK:
                    raise MeshPairingServiceError(
                        "not_resyncable", "terminal target receipt no longer proves e+2"
                    )
                fresh = await self.store.get_session(pair_id)
                return await self._final_ack_and_activate(
                    pair_id, fresh if fresh is not None else terminal_receipt
                )
            awaiting = await self._import_and_await(
                transferring, signed_env, boot_payload=boot_payload
            )
            return await self._final_ack_and_activate(pair_id, awaiting)

    async def abandon(self, pair_id: str) -> dict:
        """Target-side give-up. The reservation lives in the TARGET's store, so a
        source-side ``evict`` cannot release it cross-instance; this releases the
        target's OWN reservation, tears its (possibly imported/UNSAFE) space back to
        blank so it is reusable, and cancels the target session.

        Fail-closed: it acts ONLY after the source's SIGNED status proves the source
        is no longer enrolling this target (a terminal/non-serving state), so a
        still-convergeable pairing is never torn down."""

        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.TARGET.value:
                raise MeshPairingServiceError("unknown_pair", "unknown target pairing")
            if session.is_terminal():
                raise MeshPairingServiceError("not_abandonable", "pairing is already terminal")
            # Verify the source gave up: its signed status must report a terminal /
            # non-serving state. A serving source (issued/claimed/approved/
            # transferring/awaiting_acks/active) means the pairing may still
            # converge, so the target must NOT tear itself down.
            try:
                status_resp = await self._client(session.source_endpoint).status(
                    space_id=session.space_id,
                    epoch=session.base_epoch,
                    target_fingerprint=session.source_fingerprint,
                    pair_id=pair_id,
                )
                status_body = self._verify_source_response(
                    status_resp, source_fingerprint=session.source_fingerprint, correlation_id=pair_id
                )
                status = canonical_loads(status_body)
            except MeshPairingServiceError:
                raise
            except Exception as exc:
                raise MeshPairingServiceError(
                    "source_unverified", "cannot verify the source gave up"
                ) from exc
            state = status.get("state") if isinstance(status, dict) else None
            if state not in (
                MeshPairingState.CANCELLED.value,
                MeshPairingState.REFUSED.value,
                MeshPairingState.EXPIRED.value,
            ):
                raise MeshPairingServiceError(
                    "source_still_enrolling", "source has not given up; abandon refused"
                )
            encoded_disposition = (
                status.get("terminal_disposition_b64")
                if isinstance(status, dict)
                else None
            )
            if type(encoded_disposition) is not str:
                raise MeshPairingServiceError(
                    "source_still_enrolling",
                    "source terminal disposition is unavailable",
                )
            try:
                disposition = SignedSourceTerminalDispositionReceipt.from_bytes(
                    _unb64(encoded_disposition)
                )
            except Exception as exc:
                raise MeshPairingServiceError(
                    "source_unverified",
                    "source terminal disposition is invalid",
                ) from exc
            if not await self._target_terminal_disposition_matches_session(
                disposition, session
            ):
                raise MeshPairingServiceError(
                    "source_unverified",
                    "source terminal disposition does not match this target",
                )
            # Tear the target space back to blank and release the reservation so the
            # space is reusable — but ONLY while THIS pairing still owns the space
            # reservation. If it was already released (idempotent re-abandon) or the
            # space has since been re-paired by a newer pairing, an unconditional
            # teardown would destroy that other pairing's live data; skip it and just
            # finalize this stale session. The ownership re-check + teardown + release
            # are held ATOMIC under the SPACE lock — the same lock accept_invitation
            # holds across its blank-check + reserve — so no concurrent re-pair can
            # interleave between the check and the teardown (a bare point-in-time
            # get_reservation would be a check-to-teardown TOCTOU). Across processes,
            # only the single Mesh process-identity leader serves mutations (the peer
            # router and the admin middleware both refuse when the lock is not held),
            # so this in-process lock is the whole serialization.
            async with self.store.space_lock(session.space_id):
                reservation = await self.store.get_reservation(session.space_id)
                if reservation == pair_id:
                    await self._teardown_target_space(session.space_id)
                    await self._release_target_pairing_fence(session)
                    await self.store.release(session.space_id, pair_id)
                elif reservation is None:
                    # A raw reservation can be lost independently of the #417
                    # direct target authority.  Do not turn the session into a
                    # generic CANCELLED record and let a later cancel retry
                    # release its held fence: first prove this is still this
                    # target's no-newer-owner recovery tail, then tear the
                    # imported space back to blank before the signed release.
                    # A true legacy tail has no such proof and remains
                    # fail-closed for explicit operator recovery.
                    if await self._target_acceptance_intent_matches_session(session) is not True:
                        raise MeshPairingServiceError(
                            "target_reservation_lost",
                            "target reservation is missing without #417 ownership proof",
                        )
                    authorities = await asyncio.gather(
                        self.store.get_target_pairing_protocol_floor(session.space_id),
                        self.store.get_target_pairing_current_tail(session.space_id),
                        self.store.get_target_pairing_fence(session.space_id),
                    )
                    if any(signed is None for signed in authorities):
                        raise MeshPairingServiceError(
                            "target_reservation_lost",
                            "target pairing recovery authority is incomplete",
                        )
                    for signed in authorities:
                        assert signed is not None
                        try:
                            signed.verify(self._config.public_key)
                        except Exception as exc:
                            raise MeshPairingServiceError(
                                "target_fence_invalid",
                                "target pairing recovery fence is unavailable",
                            ) from exc
                        if (
                            signed.authority.space_id != session.space_id
                            or signed.authority.pair_id != session.pair_id
                            or signed.authority.target_fingerprint
                            != self._config.fingerprint
                            or signed.authority.target_public_key
                            != self._config.public_key
                            or signed.authority.phase not in ("held", "released")
                        ):
                            raise MeshPairingServiceError(
                                "target_reservation_lost",
                                "target space has a different or terminal pairing owner",
                            )
                    await self._teardown_target_space(session.space_id)
                    try:
                        await self._bootstrap()._assert_blank_target(session.space_id)
                    except BootstrapError as exc:
                        raise MeshPairingServiceError(
                            "target_reservation_lost",
                            "target space did not become blank before release",
                        ) from exc
                    await self._release_target_pairing_fence(session)
            if session.state in (
                MeshPairingState.TRANSFERRING.value,
                MeshPairingState.AWAITING_ACKS.value,
            ):
                session = await self._block_recovery(
                    session, phase="source_evicted", next_action="evict"
                )
            cancelled = session.transition(MeshPairingState.CANCELLED, now_ms=self._clock_ms())
            await self.store.put_session(cancelled)
        return {"pair_id": pair_id, "state": MeshPairingState.CANCELLED.value}

    async def _fetch_and_verify_approval(
        self, session: MeshPairingSession, *, require_current_source: bool = False
    ) -> SignedMeshBootstrapEnvelope:
        """Fetch the source's status, verify its signed response, the signed
        approval + bootstrap envelope, and the invitation/claim/approval chain.
        Returns the verified signed bootstrap envelope (payload fetched later)."""

        client = self._client(session.source_endpoint)
        status_resp = await client.status(
            space_id=session.space_id,
            epoch=session.base_epoch,
            target_fingerprint=session.source_fingerprint,
            pair_id=session.pair_id,
        )
        if status_resp.status_code != 200:
            raise MeshPairingServiceError("not_ready", "source has not approved yet")
        status_body = self._verify_source_response(
            status_resp, source_fingerprint=session.source_fingerprint, correlation_id=session.pair_id
        )
        status = canonical_loads(status_body)
        if not isinstance(status, dict) or "approval_b64" not in status or "bootstrap_envelope_b64" not in status:
            raise MeshPairingServiceError("not_ready", "source has not approved yet")
        if require_current_source and status.get("source_bootstrap_ready") is not True:
            raise MeshPairingServiceError(
                "source_not_ready", "source is not currently safe for target resync"
            )
        signed_approval = SignedMeshArtifact.from_bytes(_unb64(status["approval_b64"]))
        signed_env = SignedMeshBootstrapEnvelope.from_bytes(_unb64(status["bootstrap_envelope_b64"]))
        signed_env.verify()
        invitation_bytes = await self.store.get_blob(session.pair_id, "invitation")
        claim_bytes = await self.store.get_blob(session.pair_id, "claim")
        if invitation_bytes is None or claim_bytes is None:
            raise MeshPairingServiceError("missing_artifacts", "target artifacts are missing")
        invitation = SignedMeshArtifact.from_bytes(invitation_bytes)
        claim = SignedMeshArtifact.from_bytes(claim_bytes)
        verify_artifact_chain(invitation, claim, signed_approval)
        approval = signed_approval.artifact
        if (
            not isinstance(approval, MeshEnrollmentApproval)
            or approval.pair_id != session.pair_id
            or approval.space_id != session.space_id
            or approval.membership_epoch != session.base_epoch
            or approval.target_public_key != self._config.public_key
            or approval.target_fingerprint != self._config.fingerprint
        ):
            raise MeshPairingServiceError(
                "bad_binding", "approval is not bound to this target"
            )
        # A valid source signature is NOT sufficient: bind the envelope to THIS
        # pairing so a validly-signed bootstrap for another space or addressed to a
        # different target cannot be imported into this reserved target space. The
        # target fingerprint is our own; the epoch is the post-admission e+1.
        env = signed_env.envelope
        if (
            env.space_id != session.space_id
            or env.source_public_key != approval.source_public_key
            or env.source_fingerprint != approval.source_fingerprint
            or env.target_fingerprint != approval.target_fingerprint
            or env.target_fingerprint != self._config.fingerprint
            or env.membership_epoch != approval.membership_epoch + 1
        ):
            raise MeshPairingServiceError("bad_binding", "bootstrap envelope is not bound to this pairing")
        # Retain the complete signed chain root before the imported state may be
        # used.  Later activation tails authenticate the original source against
        # these bytes, never against a mutable target session field.
        await self.store.put_blob(
            session.pair_id, "validated_approval", signed_approval.canonical_bytes()
        )
        return signed_env

    async def _fetch_verified_bootstrap_payload(
        self,
        session: MeshPairingSession,
        *,
        signed_env: SignedMeshBootstrapEnvelope | None = None,
    ) -> bytes:
        """Fetch one source-signed bootstrap payload for an already verified pair."""

        boot_resp = await self._client(session.source_endpoint).fetch_bootstrap(
            space_id=session.space_id,
            epoch=session.base_epoch,
            target_fingerprint=session.source_fingerprint,
            pair_id=session.pair_id,
        )
        if boot_resp.status_code != 200:
            raise MeshPairingServiceError("no_bootstrap", "source has no bootstrap")
        payload = self._verify_source_response(
            boot_resp,
            source_fingerprint=session.source_fingerprint,
            correlation_id=session.pair_id,
        )
        if signed_env is not None:
            env = signed_env.envelope
            if payload_digest(payload) != env.payload_digest:
                raise MeshPairingServiceError(
                    "bootstrap_mismatch", "source bootstrap payload does not match approval"
                )
            try:
                snapshot = parse_snapshot_payload(
                    payload,
                    max_objects=self._config.bootstrap_max_objects,
                    max_bytes=self._config.bootstrap_max_bytes,
                )
            except MeshBootstrapError as exc:
                raise MeshPairingServiceError(
                    "bootstrap_mismatch", "source bootstrap payload is invalid"
                ) from exc
            manifest = snapshot.manifest
            if (
                manifest.manifest_sha256 != env.manifest_digest
                or manifest.membership_epoch != env.membership_epoch
                or manifest.bank_version != env.bank_version
            ):
                raise MeshPairingServiceError(
                    "bootstrap_mismatch", "source bootstrap manifest does not match approval"
                )
        return payload

    async def _import_and_await(
        self,
        transferring: MeshPairingSession,
        signed_env: SignedMeshBootstrapEnvelope,
        *,
        boot_payload: bytes | None = None,
    ) -> MeshPairingSession:
        """Fetch + import the bootstrap into a TRANSFERRING target, then advance to
        AWAITING_ACKS. Any post-admission import failure is fail-closed into
        ``blocked_recovery`` with signed ``resync`` evidence — never a raw error
        that could strand the source's pending member."""

        base = transferring.base_epoch
        try:
            if boot_payload is None:
                boot_payload = await self._fetch_verified_bootstrap_payload(
                    transferring
                )
            import_result = await import_bootstrap(
                self._bootstrap(),
                transferring.space_id,
                signed_envelope=signed_env,
                payload=boot_payload,
                local_keypair=_LocalKeypair(_legacy_membership_key(self._config.public_key)),
                expected_source_public_key=transferring.source_public_key,
                expected_epoch=base + 1,
                max_objects=self._config.bootstrap_max_objects,
                max_bytes=self._config.bootstrap_max_bytes,
            )
            # Retain the exact source-signed e+1 authority locally.  The
            # import marker below is operational state; every later activation
            # tail re-verifies these bytes before trusting that marker.
            await self.store.put_blob(
                transferring.pair_id,
                "validated_bootstrap_envelope",
                signed_env.canonical_bytes(),
            )
            await self.store.put_blob(
                transferring.pair_id,
                "validated_bootstrap_payload",
                boot_payload,
            )
            # A session merely records workflow progress.  Persist a separate,
            # read-back-verified authority that proves this exact signed e+1
            # import reached this target before it may self-promote.
            await self._persist_import_validation(
                transferring, signed_env, import_result
            )
        except Exception as exc:
            fresh = await self._block_target_import_failure(
                transferring, manifest_digest=signed_env.envelope.manifest_digest
            )
            if fresh.state == MeshPairingState.ACTIVE.value:
                return fresh
            raise MeshPairingServiceError(
                "import_failed", "bootstrap import failed; pairing is in blocked recovery"
            ) from exc
        # The source may re-deliver e+2 while a resync is between import
        # readback and this workflow-state write.  Serialize this small local
        # tail with target finalization: otherwise a stale TRANSFERRING object
        # can overwrite the just-written terminal ACTIVE receipt.
        async with self.store.space_lock(transferring.space_id):
            fresh = await self.store.get_session(transferring.pair_id)
            if fresh is None or fresh.role != MeshPairingRole.TARGET.value:
                raise MeshPairingServiceError(
                    "import_failed", "target pairing state disappeared during import"
                )
            if fresh.state == MeshPairingState.ACTIVE.value:
                return fresh
            if fresh.state == MeshPairingState.TRANSFERRING.value:
                awaiting = fresh.transition(
                    MeshPairingState.AWAITING_ACKS, now_ms=self._clock_ms()
                )
                await self.store.put_session(awaiting)
                return awaiting
            if fresh.state == MeshPairingState.AWAITING_ACKS.value:
                return fresh
            # A concurrent recovery decision is authoritative over this stale
            # import worker.  Do not overwrite its blocked/cancelled record.
            raise MeshPairingServiceError(
                "import_failed", "target pairing changed during import"
            )

    async def _final_ack_and_activate(self, pair_id: str, session: MeshPairingSession) -> dict:
        """Send the final ACK to the source. Its ACK handler runs Transition 2 and
        delivers the e+2 activation back here (self-activated through the confined
        router branch) before returning, so on return the session is ``active``."""

        ack_body = canonical_dumps(
            {
                "epoch": session.base_epoch + 1,
                "bank_version": session.bootstrap_bank_version,
                "manifest_digest": session.bootstrap_manifest_digest,
            }
        )
        ack_resp = await self._client(session.source_endpoint).ack(
            space_id=session.space_id,
            epoch=session.base_epoch + 1,
            target_fingerprint=session.source_fingerprint,
            pair_id=pair_id,
            body=ack_body,
        )
        final = await self.store.get_session(pair_id)
        return {
            "pair_id": pair_id,
            "state": final.state if final is not None else session.state,
            "ack_status": ack_resp.status_code,
        }

    async def _teardown_target_space(self, space_id: str) -> None:
        """Delete every non-placeholder object under a target space so a corrupt
        partial import can be re-imported into a blank space. The ``_hivemind/
        node_status.json`` UNSAFE marker is deleted LAST (after ``node.json`` /
        ``members.json`` and everything else), so a crash mid-teardown can never
        leave the space structurally-complete-but-unmarked (which would classify as
        HEALTHY). Fails closed: ``_assert_blank_target`` must pass afterwards."""

        if type(space_id) is not str or _SOURCE_SPACE_ID_RE.fullmatch(space_id) is None:
            raise MeshPairingServiceError(
                "invalid_space_id", "target recovery space id is invalid"
            )
        storage = self._storage_factory()
        prefix = f"{space_id}/"
        placeholders = {"", "_meta.json", "_rules.md", "live/.keep", "bank/.keep"}
        status_rel = "_hivemind/node_status.json"
        teardown_cap = self._config.bootstrap_max_objects + len(placeholders) + 1
        objects = await storage.list_objects(prefix, max_keys=teardown_cap)
        if len(objects) >= teardown_cap:
            raise MeshPairingServiceError(
                "resync_inventory_too_large",
                "target recovery inventory exceeds its automatic safety bound",
            )
        deferred: Optional[str] = None
        for obj in objects:
            key = obj["Key"]
            rel = key[len(prefix):]
            if rel in placeholders:
                continue
            if rel == status_rel:
                deferred = key  # the UNSAFE marker is removed LAST
                continue
            await storage.delete(key)
        if deferred is not None:
            await storage.delete(deferred)
        try:
            await self._bootstrap()._assert_blank_target(space_id)
        except BootstrapError as exc:
            # Do not leak the offending storage paths BootstrapError carries.
            raise MeshPairingServiceError(
                "resync_not_blank", "target could not be reset to a blank space"
            ) from exc

    # ==================================================================
    # SOURCE — inbound peer routes (called by the router)
    # ==================================================================

    async def handle_event(self, envelope: MeshRequestEnvelope, event) -> HandlerResult:
        """Apply a general same-epoch shared event from an authorized active peer.

        The router has already proven the source is a unique active member, the
        epoch matches the local view exactly, and replay is admitted.  Appending
        the event to the local journal is idempotent (deterministic event_id
        dedup), demonstrating the paired mesh carries subsequent shared mutations.
        """

        # Idempotent e+2 activation re-confirmation: a source re-driving its own
        # activation (resume after a crash before persisting ACTIVE) must receive
        # the SAME signed active confirmation, not a generic ack, or it can never
        # confirm convergence. Every other event falls through to the append.
        reconfirm = await self.try_activation_reconfirmation(envelope, event)
        if reconfirm is not None:
            return reconfirm
        store = self._hive_store(envelope.space_id)
        await store.append_event(event)
        return HandlerResult(
            MeshResponseCode.ACCEPTED,
            canonical_dumps({"applied": True, "event_id": event.event_id}),
        )

    async def handle_pair_request(
        self, envelope: MeshRequestEnvelope, body: bytes
    ) -> HandlerResult:
        """Dispatch an authenticated pair-route request to its handler."""

        if envelope.op is MeshHttpOperation.PAIR_CLAIM:
            return await self._handle_claim(envelope, body)
        if envelope.op is MeshHttpOperation.PAIR_STATUS:
            return await self._handle_status(envelope)
        if envelope.op is MeshHttpOperation.PAIR_BOOTSTRAP:
            return await self._handle_bootstrap(envelope)
        if envelope.op is MeshHttpOperation.PAIR_ACK:
            return await self._handle_ack(envelope, body)
        return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)

    async def _handle_claim(self, envelope: MeshRequestEnvelope, body: bytes) -> HandlerResult:
        pair_id = envelope.request_id
        session = await self.store.get_session(pair_id)
        if session is None or session.role != MeshPairingRole.SOURCE.value:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        if session.state not in (
            MeshPairingState.ISSUED.value,
            MeshPairingState.CLAIMED.value,
            MeshPairingState.APPROVED.value,
            MeshPairingState.CANCELLED.value,
        ):
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        now = self._clock_ms()
        try:
            decoded = canonical_loads(body)
        except Exception:
            return _refuse(MeshResponseCode.INVALID_EVENT)
        if not isinstance(decoded, dict) or set(decoded) != {"claim", "secret", "target_endpoint"}:
            return _refuse(MeshResponseCode.INVALID_EVENT)
        try:
            signed_claim = SignedMeshArtifact.from_bytes(canonical_dumps(decoded["claim"]))
            signed_claim.verify()
        except MeshArtifactError:
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        claim = signed_claim.artifact
        if type(claim) is not MeshJoinClaim:
            return _refuse(MeshResponseCode.INVALID_EVENT)
        # Bind the claim to this invitation/session and prove the sender is the
        # target that signed the claim.
        if (
            claim.pair_id != pair_id
            or claim.space_id != session.space_id
            or claim.source_public_key != session.source_public_key
            or claim.source_fingerprint != session.source_fingerprint
            or claim.invitation_digest != session.invitation_digest
            or envelope.source_public_key != claim.target_public_key
            or envelope.source_fingerprint != claim.target_fingerprint
        ):
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        if claim.membership_epoch != session.base_epoch:
            return _refuse(MeshResponseCode.EPOCH_MISMATCH)
        secret = decoded["secret"]
        if (
            not await self._source_invitation_secret_matches_session(session)
            or type(secret) is not str
            or not verify_invitation_secret(
                secret,
                session.secret_digest,
                pair_id=pair_id,
                space_id=session.space_id,
            )
        ):
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        endpoint = decoded["target_endpoint"]
        if type(endpoint) is not str or not endpoint:
            return _refuse(MeshResponseCode.INVALID_EVENT)
        try:
            canonical_target_endpoint = self._canonical_peer_endpoint(
                endpoint, peer="target"
            )
        except MeshPairingServiceError:
            return _refuse(MeshResponseCode.INVALID_EVENT)
        # Serialize the burn + session mutation so two concurrent claims bearing
        # the same valid secret cannot both proceed (one-time atomicity, no
        # last-writer-wins on the target binding). The per-pair lock is the
        # session tier of the session->reservation->membership order; this handler
        # makes no outbound call while holding it.
        async with self.store.pair_lock(pair_id):
            fresh = await self.store.get_session(pair_id)
            if fresh is None or fresh.role != MeshPairingRole.SOURCE.value:
                return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
            if not await self._source_invitation_secret_matches_session(fresh):
                return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
            # A pre-claim cancellation barrier is an abort monotone.  It is
            # intentionally independent of the mutable operational session so
            # a crash after the barrier but before ``CANCELLED`` is persisted
            # (or a valid-schema replay of ISSUED/CLAIMED) cannot revive normal
            # admission and strand the target's held reservation.  Normalize
            # every still pre-T1 source state to the sole cleanup state before
            # *any* claimed/issued retry or expiry routing.  The barrier plus
            # exact current membership is the authority for this exceptional
            # state repair; all target-facing fields remain untrusted and are
            # cleared before the exact incoming claim is rebound below.
            barrier = await self.store.get_source_preclaim_cancel_barrier(pair_id)
            if barrier is not None:
                async with self.store.space_lock(fresh.space_id):
                    membership_svc = self._membership(fresh.space_id)
                    async with membership_svc.space_lock(), token_mutation_lock(
                        fresh.space_id
                    ):
                        current = await self.store.get_session(pair_id)
                        if (
                            current is None
                            or current.role != MeshPairingRole.SOURCE.value
                            or current.state
                            not in (
                                MeshPairingState.ISSUED.value,
                                MeshPairingState.CLAIMED.value,
                                MeshPairingState.APPROVED.value,
                                MeshPairingState.CANCELLED.value,
                            )
                        ):
                            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
                        membership = await self._hive_store(
                            current.space_id
                        ).get_membership()
                        if (
                            membership is None
                            or not await self._source_preclaim_cancel_barrier_matches(
                                barrier, current, membership
                            )
                        ):
                            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
                        existing_disposition = (
                            await self.store.get_source_terminal_disposition(pair_id)
                        )
                        if (
                            current.state != MeshPairingState.CANCELLED.value
                            or existing_disposition is None
                        ):
                            # The barrier was minted only from an ISSUED,
                            # membership-base-matching source.  A target/claim
                            # binding observed afterwards cannot be used as an
                            # admission authority.  Unless a complete signed
                            # terminal disposition already binds the current
                            # target fields, retain at most a matching immutable
                            # claim blob for the narrow cleanup retry and erase
                            # every mutable target-facing field.
                            cancelled_updates = (
                                self._preclaim_cancelled_session_updates()
                            )
                            if current.state == MeshPairingState.CANCELLED.value:
                                current = current.with_fields(
                                    now_ms=now, **cancelled_updates
                                )
                            else:
                                current = current.transition(
                                    MeshPairingState.CANCELLED,
                                    now_ms=now,
                                    **cancelled_updates,
                                )
                            await self.store.put_session(current)
                        fresh = current
            # Expiry prevents a claim from ever starting admission.  It does
            # not prevent the narrow cleanup-only path above: the barrier
            # routes an exact delayed claim through CANCELLED even when the
            # original invitation lifetime has elapsed.
            if (
                fresh.state == MeshPairingState.ISSUED.value
                and now >= fresh.expires_at_ms
            ):
                return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
            if fresh.state == MeshPairingState.CLAIMED.value:
                retained_claim = await self.store.get_blob(pair_id, "claim")
                # Sessions created before #417 could retain a syntactically
                # equivalent but non-canonical HTTPS endpoint (for example an
                # explicit default port).  Normalize only after the complete
                # signed claim/session binding below has matched; a malformed
                # legacy value remains fail-closed rather than being reused.
                try:
                    stored_target_endpoint = self._canonical_peer_endpoint(
                        fresh.target_endpoint, peer="target"
                    )
                except MeshPairingServiceError:
                    return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
                if (
                    fresh.target_public_key == claim.target_public_key
                    and fresh.target_fingerprint == claim.target_fingerprint
                    and stored_target_endpoint == canonical_target_endpoint
                    and fresh.claim_digest == signed_claim.digest()
                    and retained_claim == signed_claim.canonical_bytes()
                ):
                    # Lost response retry: the source already burned the secret
                    # and recorded this exact claim.  Re-ack without changing
                    # nonce/session/artifacts, so target Action 2 is idempotent.
                    if fresh.target_endpoint != stored_target_endpoint:
                        fresh = fresh.with_fields(
                            now_ms=self._clock_ms(),
                            target_endpoint=stored_target_endpoint,
                        )
                        await self.store.put_session(fresh)
                    return _ok({"pair_id": pair_id, "state": fresh.state})
                return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
            if fresh.state == MeshPairingState.CANCELLED.value:
                # Source cancellation can win while an otherwise valid target
                # claim is already in transport.  The source was ISSUED at the
                # time of cancellation, so it could not yet sign the ordinary
                # target-bound disposition.  A signed pre-claim barrier is the
                # sole authority that permits binding THIS exact claim once,
                # without admitting it or reviving the cancelled pairing.
                #
                # First preserve a lost-response retry for an already-bound
                # disposition.  It is safe only when every mutable field and
                # retained claim match the incoming signed claim exactly.
                retained_claim = await self.store.get_blob(pair_id, "claim")
                existing_disposition = await self.store.get_source_terminal_disposition(
                    pair_id
                )
                if (
                    retained_claim == signed_claim.canonical_bytes()
                    and fresh.target_public_key == claim.target_public_key
                    and fresh.target_fingerprint == claim.target_fingerprint
                    and fresh.target_endpoint == canonical_target_endpoint
                    and fresh.claim_digest == signed_claim.digest()
                    and existing_disposition is not None
                    and await self._source_terminal_disposition_matches_session(
                        existing_disposition, fresh
                    )
                    and existing_disposition.receipt.disposition == "pre_t1_cancel"
                ):
                    return _ok(
                        {
                            "pair_id": pair_id,
                            "state": MeshPairingState.CANCELLED.value,
                            "terminal_disposition_b64": _b64(
                                existing_disposition.canonical_bytes()
                            ),
                        }
                    )

                # A crash can leave either the exact claim blob, or the exact
                # claim/session binding, durable before its target-facing
                # disposition.  Preserve only those two retry prefixes;
                # every other partial binding is mutable operational state and
                # must not choose a target or replace a conflicting claim.
                bound_to_claim = (
                    retained_claim == signed_claim.canonical_bytes()
                    and fresh.target_public_key == claim.target_public_key
                    and fresh.target_fingerprint == claim.target_fingerprint
                    and fresh.target_endpoint == canonical_target_endpoint
                    and fresh.claim_digest == signed_claim.digest()
                    and not fresh.approval_digest
                    and not fresh.bootstrap_manifest_digest
                    and fresh.bootstrap_bank_version == -1
                )
                pristine_cancelled = (
                    retained_claim is None
                    and not fresh.target_public_key
                    and not fresh.target_fingerprint
                    and not fresh.target_endpoint
                    and not fresh.claim_digest
                    and not fresh.approval_digest
                    and not fresh.bootstrap_manifest_digest
                    and fresh.bootstrap_bank_version == -1
                )
                claim_blob_only = (
                    retained_claim == signed_claim.canonical_bytes()
                    and not fresh.target_public_key
                    and not fresh.target_fingerprint
                    and not fresh.target_endpoint
                    and not fresh.claim_digest
                    and not fresh.approval_digest
                    and not fresh.bootstrap_manifest_digest
                    and fresh.bootstrap_bank_version == -1
                )
                if not (bound_to_claim or pristine_cancelled or claim_blob_only):
                    return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)

                # Match cancel's lock order before reading the source view and
                # minting the target-bound release proof.  This prevents an
                # ordinary membership mutation from changing the pre-T1 view
                # between barrier verification and disposition persistence.
                async with self.store.space_lock(fresh.space_id):
                    membership_svc = self._membership(fresh.space_id)
                    async with membership_svc.space_lock(), token_mutation_lock(
                        fresh.space_id
                    ):
                        current = await self.store.get_session(pair_id)
                        if (
                            current is None
                            or current.role != MeshPairingRole.SOURCE.value
                            or current.state != MeshPairingState.CANCELLED.value
                        ):
                            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
                        membership = await self._hive_store(
                            current.space_id
                        ).get_membership()
                        if membership is None:
                            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
                        barrier = await self.store.get_source_preclaim_cancel_barrier(
                            pair_id
                        )
                        if (
                            barrier is None
                            or not await self._source_preclaim_cancel_barrier_matches(
                                barrier, current, membership
                            )
                        ):
                            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
                        # Re-read inside the serialized prefix: a crash retry
                        # may have completed the binding while this request was
                        # waiting for the locks above.
                        retained_claim = await self.store.get_blob(pair_id, "claim")
                        bound_to_claim = (
                            retained_claim == signed_claim.canonical_bytes()
                            and current.target_public_key == claim.target_public_key
                            and current.target_fingerprint == claim.target_fingerprint
                            and current.target_endpoint == canonical_target_endpoint
                            and current.claim_digest == signed_claim.digest()
                            and not current.approval_digest
                            and not current.bootstrap_manifest_digest
                            and current.bootstrap_bank_version == -1
                        )
                        pristine_cancelled = (
                            retained_claim is None
                            and not current.target_public_key
                            and not current.target_fingerprint
                            and not current.target_endpoint
                            and not current.claim_digest
                            and not current.approval_digest
                            and not current.bootstrap_manifest_digest
                            and current.bootstrap_bank_version == -1
                        )
                        claim_blob_only = (
                            retained_claim == signed_claim.canonical_bytes()
                            and not current.target_public_key
                            and not current.target_fingerprint
                            and not current.target_endpoint
                            and not current.claim_digest
                            and not current.approval_digest
                            and not current.bootstrap_manifest_digest
                            and current.bootstrap_bank_version == -1
                        )
                        if not (
                            bound_to_claim or pristine_cancelled or claim_blob_only
                        ):
                            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
                        nonce_result = await self.store.record_nonce(
                            claim.nonce,
                            pair_id=pair_id,
                            claim_digest=signed_claim.digest(),
                            now_ms=now,
                        )
                        if nonce_result not in {"new", "same"}:
                            return _refuse(MeshResponseCode.REPLAY_REJECTED)
                        if pristine_cancelled:
                            await self.store.put_blob(
                                pair_id, "claim", signed_claim.canonical_bytes()
                            )
                        if not await self.store.is_secret_burned(
                            pair_id, secret_digest=current.secret_digest
                        ):
                            await self.store.burn_secret(
                                pair_id, current.secret_digest, now_ms=now
                            )
                        if bound_to_claim:
                            bound = current
                        else:
                            granted = tuple(
                                sorted(
                                    (set(claim.requested_scopes) & set(current.granted_scopes))
                                    | {"read"}
                                )
                            )
                            bound = current.with_fields(
                                now_ms=now,
                                target_public_key=claim.target_public_key,
                                target_fingerprint=claim.target_fingerprint,
                                target_endpoint=canonical_target_endpoint,
                                claim_digest=signed_claim.digest(),
                                granted_scopes=granted,
                            )
                            await self.store.put_session(bound)
                        disposition = await self._persist_source_terminal_disposition(
                            bound,
                            disposition="pre_t1_cancel",
                            membership=membership,
                        )
                return _ok(
                    {
                        "pair_id": pair_id,
                        "state": MeshPairingState.CANCELLED.value,
                        "terminal_disposition_b64": _b64(
                            disposition.canonical_bytes()
                        ),
                    }
                )
            if fresh.state != MeshPairingState.ISSUED.value:
                return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
            # First bind the global nonce to THIS exact signed claim.  This is
            # intentionally before the pair-local blob: a foreign pair that
            # reuses a consumed nonce must leave no artifact behind that could
            # poison a later honest retry.  A crash after this write is safely
            # recoverable only by the same pair/digest below.
            retained_claim = await self.store.get_blob(pair_id, "claim")
            if retained_claim is not None and retained_claim != signed_claim.canonical_bytes():
                return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
            nonce_result = await self.store.record_nonce(
                claim.nonce,
                pair_id=pair_id,
                claim_digest=signed_claim.digest(),
                now_ms=now,
            )
            if nonce_result == "different":
                return _refuse(MeshResponseCode.REPLAY_REJECTED)
            # The pair-local artifact is the second immutable recovery anchor.
            # A retry after either prefix (nonce or blob) can only complete
            # because both records name this exact signed claim.
            if retained_claim is None:
                await self.store.put_blob(
                    pair_id, "claim", signed_claim.canonical_bytes()
                )
            elif nonce_result not in {"new", "same"}:  # defensive closed set
                return _refuse(MeshResponseCode.REPLAY_REJECTED)
            if not await self.store.is_secret_burned(
                pair_id, secret_digest=fresh.secret_digest
            ):
                await self.store.burn_secret(
                    pair_id, fresh.secret_digest, now_ms=now
                )
            granted = tuple(
                sorted((set(claim.requested_scopes) & set(fresh.granted_scopes)) | {"read"})
            )
            updated = fresh.transition(
                MeshPairingState.CLAIMED,
                now_ms=now,
                target_public_key=claim.target_public_key,
                target_fingerprint=claim.target_fingerprint,
                target_endpoint=canonical_target_endpoint,
                claim_digest=signed_claim.digest(),
                granted_scopes=granted,
            )
            await self.store.put_session(updated)
        return _ok({"pair_id": pair_id, "state": updated.state})

    async def approve(self, pair_id: str) -> dict:
        """Approve a claimed pairing: sign approval, admit pending (e->e+1), export
        the bounded signed bootstrap (source side, Action 3.1).

        Serialized per pair (a retried/concurrent approve of the SAME pairing cannot
        overwrite a post-mutation session) AND per SPACE (the same lock
        accept_invitation holds across reserve): two concurrent approvals of
        DIFFERENT invitations from the same single-member source would otherwise
        both pass the sole-active-member / base-epoch check and admit two PENDING
        candidates at successive epochs, stranding both bootstraps (each session
        expects base_epoch+1 but the second admit exports base_epoch+2). Holding the
        space lock across the whole check -> admit -> export makes the second
        approval re-read the bumped epoch and fail its epoch check."""

        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.SOURCE.value:
                raise MeshPairingServiceError("unknown_pair", "unknown source pairing")
            async with self.store.space_lock(session.space_id):
                return await self._approve_locked(pair_id)

    async def _approve_locked(self, pair_id: str) -> dict:
        session = await self.store.get_session(pair_id)
        if session is None or session.role != MeshPairingRole.SOURCE.value:
            raise MeshPairingServiceError("unknown_pair", "unknown source pairing")
        disposition = await self.store.get_source_terminal_disposition(pair_id)
        if disposition is not None:
            if not await self._source_terminal_disposition_matches_session(
                disposition, session
            ):
                raise MeshPairingServiceError(
                    "terminal_disposition_unavailable",
                    "source terminal disposition is invalid",
                )
            # A disposition is written before the mutable source session becomes
            # CANCELLED.  It is an abort monotone, not merely a target-facing
            # hint: after a crash in that gap, a stale approve must never admit
            # the candidate whose target already tore down its reservation.
            raise MeshPairingServiceError(
                "not_cancellable",
                "pairing has a durable terminal disposition",
            )
        # A source-only ISSUED cancellation barrier is equally monotone.  It
        # deliberately exists before a target identity can be known, so a
        # crash after its durable write but before the source session reaches
        # CANCELLED must still prevent a tampered/retried CLAIMED session from
        # admitting a candidate whose target may already have abandoned.
        barrier = await self.store.get_source_preclaim_cancel_barrier(pair_id)
        if barrier is not None:
            membership = await self._hive_store(session.space_id).get_membership()
            if (
                membership is None
                or not await self._source_preclaim_cancel_barrier_matches(
                    barrier, session, membership
                )
            ):
                raise MeshPairingServiceError(
                    "terminal_disposition_unavailable",
                    "source pre-claim cancellation barrier is invalid",
                )
            raise MeshPairingServiceError(
                "not_cancellable",
                "pairing has a durable pre-claim cancellation barrier",
            )
        if session.state != MeshPairingState.CLAIMED.value:
            raise MeshPairingServiceError("bad_state", "pairing is not awaiting approval")
        store = self._hive_store(session.space_id)
        membership = await store.get_membership()
        node = await store.get_node_identity()
        if membership is None or node is None:
            raise MeshPairingServiceError("not_meshable", "space is not an eligible Mesh source")
        if membership.epoch != session.base_epoch:
            raise MeshPairingServiceError("epoch_changed", "membership epoch changed since invitation")
        if len(active_members(membership)) != 1:
            raise MeshPairingServiceError(
                "multi_member_source", "V1 Mesh pairing requires a single active-member source"
            )
        # Single-in-flight-pairing-per-source (V1), race-safe under the store space
        # lock held by ``approve``: refuse to admit a SECOND candidate while another
        # pairing's target is still PENDING. Two admitted (PENDING) targets could
        # each reach activation and then mutually fence each other's promote/give-up
        # (neither clearable) — see the caller-bound activation fence. Checking the
        # membership itself (not session state) also catches a crash-window pairing
        # that admitted but never persisted `transferring`. The concurrent case
        # (two approvals sharing one base_epoch) is already caught by the
        # compare-and-admit `expected_epoch`; this catches the sequential case where
        # the second invitation was minted AFTER the first admitted (fresh epoch).
        if any(m.status == MemberStatus.PENDING.value for m in membership.members):
            raise MeshPairingServiceError(
                "pairing_in_flight",
                "another Mesh pairing for this space has a candidate mid-enrollment; "
                "complete, cancel, or evict it before approving another",
            )

        now = self._clock_ms()
        approval = MeshEnrollmentApproval(
            protocol_version=1,
            kind=MeshArtifactKind.ENROLLMENT_APPROVAL,
            pair_id=pair_id,
            space_id=session.space_id,
            source_public_key=session.source_public_key,
            source_fingerprint=session.source_fingerprint,
            target_public_key=session.target_public_key,
            target_fingerprint=session.target_fingerprint,
            membership_epoch=session.base_epoch,
            issued_at_ms=now,
            nonce=generate_pairing_nonce(),
            invitation_digest=session.invitation_digest,
            join_claim_digest=session.claim_digest,
            granted_scopes=tuple(session.granted_scopes),
        )
        signed_approval = SignedMeshArtifact.sign(approval, self._config.private_key)
        # Fail-closed authority check: we must be an eligible active commit-scoped
        # member of the enrollment view (T17).
        verify_approval_authority(approval, membership, enrollment_space_id=session.space_id)
        # Full chain integrity (invitation + claim + approval).
        invitation_bytes = await self.store.get_blob(pair_id, "invitation")
        claim_bytes = await self.store.get_blob(pair_id, "claim")
        if invitation_bytes is None or claim_bytes is None:
            raise MeshPairingServiceError("missing_artifacts", "pairing artifacts are missing")
        verify_artifact_chain(
            SignedMeshArtifact.from_bytes(invitation_bytes),
            SignedMeshArtifact.from_bytes(claim_bytes),
            signed_approval,
        )
        target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
        membership_svc = self._membership(session.space_id)
        # The durable session is left at 'approved' for the whole fallible export —
        # NOT prematurely persisted as 'transferring' — because 'transferring' is a
        # source dead end (no cancel/evict/resume edge), so persisting empty-digest
        # 'transferring' before the heavy export would make a HARD crash during
        # export unrecoverable. Instead:
        #   * a soft export failure transitions the IN-MEMORY 'transferring' object
        #     to blocked_recovery with signed evidence (evict gives it up cleanly);
        #   * a hard crash mid-export leaves the durable session 'approved' with the
        #     target already admitted — which cancel() refuses and evict() recovers.
        candidate = Member(
            node_id=target_node_id,
            public_key=_legacy_membership_key(session.target_public_key),
            scopes=list(session.granted_scopes),
            incarnation=pair_id,
        )
        # Hold the MEMBERSHIP lock across Transition 1 (admit) AND the bootstrap
        # export so a concurrent membership mutation (e.g. re-scope) cannot advance
        # the epoch between them and make the exported snapshot carry an epoch the
        # target rejects. The admit is a compare-and-admit at exactly base_epoch: a
        # concurrent mutation BEFORE admission fails closed (epoch_changed), and
        # holding the lock through export prevents one AFTER admission.
        async with membership_svc.space_lock(), token_mutation_lock(
            session.space_id
        ):
            # State may have changed since invitation issuance. Re-run the full
            # bounded proof while membership is locked and before e -> e+1.
            current_snapshot = await self._deep_source_bootstrap_preflight(
                session.space_id
            )
            if current_snapshot.manifest.membership_epoch != session.base_epoch:
                raise MeshPairingServiceError(
                    "epoch_changed", "membership epoch changed since invitation"
                )
            await self._assert_admission_bootstrap_capacity(
                session.space_id, current_snapshot, candidate
            )
            try:
                # Complete the one-time legacy activation-index migration before
                # APPROVED evidence or any shared membership mutation. Once the
                # per-space sentinel is durable, append-only terminal history no
                # longer participates in authority decisions.
                await self.assert_no_pairing_activation(
                    session.space_id, ignore_pair_id=pair_id
                )
            except PairingActivationError as exc:
                raise MeshPairingServiceError(
                    "pairing_in_flight",
                    "another Mesh pairing for this space is mid-activation",
                ) from exc
            except MeshPairingStoreError as exc:
                raise MeshPairingServiceError(
                    "pairing_activation_state_unavailable",
                    "Mesh activation history could not be proven safe",
                ) from exc
            # Only after every fallible pre-admission capacity proof succeeds do
            # we persist APPROVED. A limit refusal therefore leaves CLAIMED and
            # remains safely retryable with no shared mutation.
            await self.store.put_blob(
                pair_id, "approval", signed_approval.canonical_bytes()
            )
            approved_session = session.transition(
                MeshPairingState.APPROVED,
                now_ms=now,
                approval_digest=signed_approval.digest(),
            )
            await self.store.put_session(approved_session)
            # This is the signed, targeted per-space index for the source tail.
            # It is written before Transition 1 and carries its own source
            # signature, so valid-schema storage cannot downgrade a current
            # #417 tail to the legacy owner format after mutable marker/fence
            # loss.  The immutable protocol floor is written only after that
            # signed owner has read back; a crash between them is pre-T1 and
            # remains owner-evictable, never a stale-e2 split window.
            migration_authority = self._source_activation_migration_authority(
                approved_session
            )
            await self.store.put_activation_migration(
                session.space_id,
                pair_id,
                now_ms=self._clock_ms(),
                rearm_for_source_activation=True,
                source_activation_authority=migration_authority,
                replace_settled_source_activation=True,
            )
            await self.store.put_source_activation_protocol_floor(
                migration_authority
            )
            transferring = approved_session.transition(
                MeshPairingState.TRANSFERRING, now_ms=now
            )
            try:
                # Transition 1: admit the target PENDING (e -> e+1). The durable
                # per-incarnation tag (pair_id) lets a retained pairing force-evict
                # ONLY the incarnation it activated.
                await membership_svc.admit_pending_candidate_locked(
                    candidate,
                    expected_epoch=session.base_epoch,
                    activation_pair_id=pair_id,
                )
            except MembershipEpochError as exc:
                raise MeshPairingServiceError(
                    "epoch_changed", "membership epoch changed since invitation"
                ) from exc
            except PairingActivationError as exc:
                # Defense-in-depth: the single-in-flight gate above should preclude a
                # DIFFERENT pairing being mid-activation, but map to a structured
                # refusal rather than an unstructured 500 if one ever is.
                raise MeshPairingServiceError(
                    "pairing_in_flight",
                    "another Mesh pairing for this space is mid-activation",
                ) from exc
            try:
                updated = await self._export_and_store_bootstrap(pair_id, session, transferring)
            except Exception as exc:
                await self._block_recovery(
                    transferring, phase="bootstrap_export_failed", next_action="evict"
                )
                raise MeshPairingServiceError(
                    "export_failed", "bootstrap export failed; pairing is in blocked recovery"
                ) from exc
        return {"pair_id": pair_id, "state": updated.state, "epoch": session.base_epoch + 1}

    async def _export_and_store_bootstrap(
        self, pair_id: str, session: MeshPairingSession, transferring: MeshPairingSession
    ) -> MeshPairingSession:
        """Export + sign the bounded e+1 bootstrap (once — the manifest carries a
        non-deterministic ``created_at``, so it is never re-exported), persist its
        blobs, and record the manifest digest + bank version on the session."""

        snapshot = await self._bootstrap().export_snapshot(
            session.space_id,
            max_objects=self._config.bootstrap_max_objects,
            max_bytes=self._config.bootstrap_max_bytes,
        )
        signed_env, payload = build_bootstrap(
            snapshot,
            space_id=session.space_id,
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            target_fingerprint=session.target_fingerprint,
            private_key=self._config.private_key,
            max_objects=self._config.bootstrap_max_objects,
            max_bytes=self._config.bootstrap_max_bytes,
        )
        if (
            snapshot.manifest.membership_epoch != session.base_epoch + 1
            or signed_env.envelope.manifest_digest
            != snapshot.manifest.manifest_sha256
            or signed_env.envelope.bank_version != snapshot.manifest.bank_version
        ):
            raise MeshPairingServiceError(
                "source_snapshot_changed", "source snapshot authority changed"
            )
        # ``approve`` holds membership + token mutation locks across this call.
        # Persist the immutable binding before any source session can advertise
        # the bootstrap as reachable; a final ACK must later reproduce it exactly.
        evidence = await self._capture_source_bootstrap_evidence(
            session,
            membership_epoch=snapshot.manifest.membership_epoch,
            manifest_digest=snapshot.manifest.manifest_sha256,
            bank_version=snapshot.manifest.bank_version,
            commit_id=snapshot.manifest.commit_id,
            recorded_at_ms=self._clock_ms(),
        )
        signed_evidence = SignedSourceBootstrapEvidence.sign(
            evidence, self._config.private_key
        )
        await self.store.put_source_bootstrap_evidence(signed_evidence)
        # Retain a separately keyed signed provenance marker.  The primary
        # evidence binds final source revalidation; this redundant copy lets
        # ordinary-write guards distinguish a #417 terminal tail from legacy
        # history even if a mutable fence, member incarnation, or primary key
        # is lost after e+2.
        await self.store.put_source_activation_marker(signed_evidence)
        persisted_evidence = await self.store.get_source_bootstrap_evidence(pair_id)
        persisted_marker = await self.store.get_source_activation_marker(
            session.space_id
        )
        if (
            persisted_evidence is None
            or persisted_evidence != signed_evidence
            or persisted_marker is None
            or persisted_marker != signed_evidence
        ):
            raise MeshPairingServiceError(
                "source_snapshot_changed", "source snapshot authority changed"
            )
        persisted_evidence.verify(self._config.public_key)
        persisted_marker.verify(self._config.public_key)
        await self.store.put_blob(pair_id, "bootstrap_payload", payload)
        await self.store.put_blob(pair_id, "bootstrap_envelope", signed_env.canonical_bytes())
        updated = transferring.with_fields(
            now_ms=self._clock_ms(),
            bootstrap_manifest_digest=snapshot.manifest.manifest_sha256,
            bootstrap_bank_version=snapshot.manifest.bank_version,
        )
        await self.store.put_session(updated)
        return updated

    async def _handle_status(self, envelope: MeshRequestEnvelope) -> HandlerResult:
        pair_id = envelope.request_id
        session = await self.store.get_session(pair_id)
        if session is None or session.role != MeshPairingRole.SOURCE.value:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        # A pre-T1 cancellation has no enrollment approval yet.  Its dedicated
        # source-signed disposition binds the invitation/claim roots and the
        # exact target request identity, so it is the sole additional status
        # authorization path.  Every ordinary status/bootstrap request still
        # requires the approved target chain.
        disposition = await self._source_terminal_disposition_for_status(
            session, envelope
        )
        enrolled_target = await self._source_request_is_enrolled_target(
            session, envelope
        )
        if disposition is None and not enrolled_target:
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        reported_state = (
            MeshPairingState.CANCELLED.value
            if disposition is not None
            else session.state
        )
        if (
            disposition is None
            and reported_state
            in (
                MeshPairingState.CANCELLED.value,
                MeshPairingState.REFUSED.value,
                MeshPairingState.EXPIRED.value,
            )
        ):
            # A mutable terminal source session has no target-release effect.
            # Keep the target in its ordinary fail-closed path until it receives
            # a disposition signed while source membership was serialized.
            reported_state = MeshPairingState.BLOCKED_RECOVERY.value
        payload: dict[str, Any] = {"pair_id": pair_id, "state": reported_state}
        if disposition is not None:
            payload["terminal_disposition_b64"] = _b64(
                disposition.canonical_bytes()
            )
        can_serve = session.state in (
            MeshPairingState.TRANSFERRING.value,
            MeshPairingState.AWAITING_ACKS.value,
            MeshPairingState.ACTIVE.value,
        )
        if session.state == MeshPairingState.BLOCKED_RECOVERY.value:
            try:
                evidence = await self._verified_blocked_evidence(session)
                can_serve = (
                    evidence.evidence.next_action == "resume"
                    and evidence.evidence.phase == "activation_unconfirmed"
                )
            except Exception:
                can_serve = False
        if can_serve:
            # Signed response metadata lets a target resync prove the CURRENT
            # source is safe before it tears down its own space.  First import
            # remains able to retrieve the already-signed e+1 snapshot when a
            # later scope drift will be rejected by the final source fence.
            payload["source_bootstrap_ready"] = await self._source_is_healthy_for_bootstrap(
                session
            )
            # Terminal status remains available for authenticated target-side
            # abandonment even if source health is gone; only bootstrap authority
            # is withheld for an explicit unsafe/resync marker.
            if await self._source_health_marker_allows_bootstrap(session):
                approval = await self.store.get_blob(pair_id, "approval")
                env = await self.store.get_blob(pair_id, "bootstrap_envelope")
                if approval is not None:
                    payload["approval_b64"] = _b64(approval)
                if env is not None:
                    payload["bootstrap_envelope_b64"] = _b64(env)
        return _ok(payload)

    async def _handle_bootstrap(self, envelope: MeshRequestEnvelope) -> HandlerResult:
        pair_id = envelope.request_id
        session = await self.store.get_session(pair_id)
        if session is None or session.role != MeshPairingRole.SOURCE.value:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        # The signed space snapshot is served ONLY to the enrolled target.
        if not await self._source_request_is_enrolled_target(session, envelope):
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        if not await self._source_health_marker_allows_bootstrap(session):
            return _refuse(MeshResponseCode.LOCAL_UNSAFE)
        can_serve = session.state in (
            MeshPairingState.TRANSFERRING.value,
            MeshPairingState.AWAITING_ACKS.value,
            MeshPairingState.ACTIVE.value,
        )
        if session.state == MeshPairingState.BLOCKED_RECOVERY.value:
            try:
                evidence = await self._verified_blocked_evidence(session)
                can_serve = (
                    evidence.evidence.next_action == "resume"
                    and evidence.evidence.phase == "activation_unconfirmed"
                )
            except Exception:
                can_serve = False
        if not can_serve:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        payload = await self.store.get_blob(pair_id, "bootstrap_payload")
        if payload is None:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        return HandlerResult(MeshResponseCode.OK, payload)

    async def _handle_ack(self, envelope: MeshRequestEnvelope, body: bytes) -> HandlerResult:
        # Serialize per pair so a retried/concurrent final ACK cannot double-run
        # Transition 2. Holding the lock across the outbound activation delivery is
        # safe: the target self-activates under its OWN (distinct) per-pair lock.
        async with self.store.pair_lock(envelope.request_id):
            return await self._handle_ack_locked(envelope, body)

    async def _handle_ack_locked(self, envelope: MeshRequestEnvelope, body: bytes) -> HandlerResult:
        pair_id = envelope.request_id
        session = await self.store.get_session(pair_id)
        if session is None or session.role != MeshPairingRole.SOURCE.value:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        if not await self._source_request_is_enrolled_target(session, envelope):
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        try:
            decoded = canonical_loads(body)
        except Exception:
            return _refuse(MeshResponseCode.INVALID_EVENT)
        if not isinstance(decoded, dict) or set(decoded) != {"epoch", "bank_version", "manifest_digest"}:
            return _refuse(MeshResponseCode.INVALID_EVENT)
        if decoded["epoch"] != session.base_epoch + 1:
            return _refuse(MeshResponseCode.EPOCH_MISMATCH)
        if (
            decoded["bank_version"] != session.bootstrap_bank_version
            or decoded["manifest_digest"] != session.bootstrap_manifest_digest
        ):
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        # An ACTIVE session is only workflow bookkeeping.  Before a duplicate
        # final ACK may be acknowledged, re-prove the local e+2 membership and
        # re-deliver the signed activation to the target.  A syntactically
        # rewritten ACTIVE record must never suppress the very confirmation that
        # makes full-mesh all-ACK activation authoritative.
        if session.state == MeshPairingState.ACTIVE.value:
            try:
                if not await self._reconfirm_active_source_locked(session):
                    return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
            except Exception:
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            return _ok({"pair_id": pair_id, "state": session.state})
        # A target is allowed to retry its final ACK after a source crash that
        # persisted ``awaiting_acks`` before/during promotion.  Route it through
        # the same lock-held recovery primitive rather than rejecting a legal
        # all-ACK convergence retry (and never re-enter ``pair_lock``).
        if session.state in (
            MeshPairingState.AWAITING_ACKS.value,
            MeshPairingState.BLOCKED_RECOVERY.value,
        ):
            try:
                resumed = await self._resume_source_locked(session)
            except MeshPairingServiceError as exc:
                return _refuse(
                    MeshResponseCode.LOCAL_UNSAFE
                    if exc.code
                    in {
                        "source_snapshot_changed",
                        "source_unavailable",
                        "unrecoverable_epoch",
                    }
                    else MeshResponseCode.OPERATION_UNAVAILABLE
                )
            if resumed.get("state") == MeshPairingState.ACTIVE.value:
                return _ok(resumed)
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        if session.state != MeshPairingState.TRANSFERRING.value:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        # Linearize final source-state revalidation and Transition 2 with every
        # local membership / token mutation.  This is intentionally a final-live
        # revalidation fence, not a new global lock imposed on unrelated writers.
        async with self.store.space_lock(session.space_id):
            membership_svc = self._membership(session.space_id)
            async with membership_svc.space_lock(), token_mutation_lock(
                session.space_id
            ):
                session = await self.store.get_session(pair_id)
                if (
                    session is None
                    or session.role != MeshPairingRole.SOURCE.value
                    or session.state != MeshPairingState.TRANSFERRING.value
                ):
                    return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
                if not await self._source_is_healthy_for_bootstrap(session):
                    return _refuse(MeshResponseCode.LOCAL_UNSAFE)
                if not await self._source_bootstrap_evidence_matches(session):
                    await self._block_recovery(
                        session,
                        phase="bootstrap_source_changed",
                        next_action="evict",
                        manifest_digest=session.bootstrap_manifest_digest,
                    )
                    return _refuse(MeshResponseCode.LOCAL_UNSAFE)
                now = self._clock_ms()
                target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
                e2 = session.base_epoch + 2
                store = self._hive_store(session.space_id)
                membership, node, term = await asyncio.gather(
                    store.get_membership(),
                    store.get_node_identity(),
                    store.get_term(),
                )
                # Fail closed on ANY concurrent membership change: promote ONLY if
                # the source is still at exactly e+1 with the target PENDING.
                if membership is None or node is None or term is None:
                    return _refuse(MeshResponseCode.LOCAL_UNSAFE)
                if membership.epoch != session.base_epoch + 1:
                    return _refuse(MeshResponseCode.EPOCH_MISMATCH)
                target_member = next(
                    (m for m in membership.members if m.node_id == target_node_id),
                    None,
                )
                if (
                    target_member is None
                    or target_member.status != MemberStatus.PENDING.value
                ):
                    return _refuse(MeshResponseCode.EPOCH_MISMATCH)
                digest = candidate_view_digest(
                    projected_promotion_view(membership, target_node_id)
                )
                event = EventEnvelope(
                    event_id=_membership_event_id(
                        session.space_id, target_node_id, e2
                    ),
                    request_id=self._request_id_factory(),
                    type=EventType.MEMBERSHIP_UPDATED,
                    origin_node_id=node.node_id,
                    term=term.term,
                    membership_epoch=e2,
                    payload={
                        "node_id": target_node_id,
                        "epoch": e2,
                        "status": MemberStatus.ACTIVE.value,
                        "candidate_view_digest": digest,
                        "pair_id": pair_id,
                    },
                )
                event_body = canonical_dumps(event.model_dump(mode="json"))
                # Persist intent/fence before the irreversible membership write.
                awaiting = session.transition(
                    MeshPairingState.AWAITING_ACKS,
                    now_ms=now,
                    activation_event_id=event.event_id,
                )
                await self.store.put_activation_fence(
                    session.space_id, pair_id, now_ms=now
                )
                await self.store.put_session(awaiting)
                try:
                    await membership_svc.promote_pending_to_active_locked(
                        target_node_id,
                        expected_epoch=session.base_epoch + 1,
                        activation_pair_id=session.pair_id,
                    )
                except (MembershipEpochError, PairingActivationError):
                    return _refuse(MeshResponseCode.EPOCH_MISMATCH)
        completed = await self._complete_source_activation_tail(
            awaiting, event, event_body, e2=e2
        )
        if completed is None:
            # No proven target apply -> blocked_recovery with signed evidence
            # (never mark active on an unverified/absent activation confirmation).
            await self._block_recovery(
                awaiting,
                phase="activation_unconfirmed",
                next_action="resume",
                candidate_view_digest=digest,
                activation_event_id=event.event_id,
            )
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        active, source_terminal_confirmed = completed
        if not source_terminal_confirmed:
            # The source has durably reached ACTIVE, but it remains fenced until
            # the target acknowledges the source-signed terminal receipt.
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        return _ok({"pair_id": pair_id, "state": active.state, "epoch": e2})

    async def _deliver_activation_confirmation(
        self, session, event, event_body: bytes, e2: int
    ) -> dict[str, Any] | None:
        """Deliver e+2 and return a verified target confirmation body."""

        try:
            response = await self._client(session.target_endpoint).deliver_event(
                space_id=session.space_id,
                epoch=e2,
                target_fingerprint=session.target_fingerprint,
                body=event_body,
                request_id=event.request_id,
            )
            if response.status_code not in (200, 202):
                return None
            resp_body = self._verify_source_response(
                response, source_fingerprint=session.target_fingerprint, correlation_id=event.request_id
            )
            confirm = canonical_loads(resp_body)
        except Exception:
            return None
        if (
            not isinstance(confirm, dict)
            or confirm.get("state") != MeshPairingState.ACTIVE.value
            or confirm.get("epoch") != e2
        ):
            return None
        return confirm

    async def _deliver_activation(
        self, session, event, event_body: bytes, e2: int, digest: str
    ) -> bool:
        """Compatibility wrapper for callers that only require target e+2 apply."""

        return (
            await self._deliver_activation_confirmation(session, event, event_body, e2)
            is not None
        )

    async def _deliver_source_terminal_confirmation(
        self,
        session: MeshPairingSession,
        event: EventEnvelope,
        *,
        target_receipt: SignedTargetActivationReceipt,
    ) -> bool:
        """Send the source-signed all-ACK receipt and require target fence release."""

        try:
            signed = await self._persist_source_activation_receipt(
                session, event, target_receipt=target_receipt
            )
            terminal_event = self._event_with_source_activation_receipt(
                event, signed, self._request_id_factory()
            )
            confirm = await self._deliver_activation_confirmation(
                session,
                terminal_event,
                canonical_dumps(terminal_event.model_dump(mode="json")),
                session.base_epoch + 2,
            )
            return bool(
                confirm is not None
                and confirm.get("source_terminal_confirmed") is True
                and confirm.get("target_activation_receipt_digest")
                == signed.receipt.target_activation_receipt_digest
                and await self._persist_target_terminal_confirmation(
                    session, signed, confirm
                )
            )
        except Exception:
            return False

    async def _replay_source_terminal_confirmation(
        self, session: MeshPairingSession, event: EventEnvelope
    ) -> bool:
        """Re-deliver an already-completed terminal chain without re-signing it.

        A normal post-all-ACK ``BANK_COMMIT`` may legitimately move the live
        e+1 head.  Recreating the source receipt would therefore either reject
        valid work or weaken the original snapshot fence.  Instead replay the
        exact source receipt together with the target's prior signed terminal
        confirmation.  The target accepts this extra field only in its narrow
        UNSAFE/reserved recovery branch, where all three signatures and its
        exact local e+2 projection must agree.
        """

        try:
            if not await self._source_terminal_confirmation_matches(session):
                return False
            signed_source, signed_terminal = await asyncio.gather(
                self.store.get_source_activation_receipt(session.pair_id),
                self.store.get_target_terminal_confirmation(session.pair_id),
            )
            if signed_source is None or signed_terminal is None:
                return False
            approval = await self._source_enrollment_approval(session)
            signed_source.verify(self._config.public_key)
            signed_terminal.verify(approval.target_public_key)
            terminal_event = self._event_with_source_activation_receipt(
                event,
                signed_source,
                self._request_id_factory(),
                terminal_confirmation=signed_terminal,
            )
            confirmation = await self._deliver_activation_confirmation(
                session,
                terminal_event,
                canonical_dumps(terminal_event.model_dump(mode="json")),
                session.base_epoch + 2,
            )
            return bool(
                confirmation is not None
                and confirmation.get("source_terminal_confirmed") is True
                and confirmation.get("target_activation_receipt_digest")
                == signed_source.receipt.target_activation_receipt_digest
                and await self._persist_target_terminal_confirmation(
                    session, signed_source, confirmation
                )
            )
        except Exception:
            return False

    async def _complete_source_activation_tail(
        self,
        awaiting: MeshPairingSession,
        event: EventEnvelope,
        event_body: bytes,
        *,
        e2: int,
    ) -> tuple[MeshPairingSession, bool] | None:
        """Close source e+2 delivery with a durable second all-ACK confirmation.

        The first target response proves that the target durably applied e+2.
        Before changing its own session to ACTIVE, the source advances its
        activation fence to the terminal-confirmation phase.  That phase keeps
        ordinary source writes refused across a crash until the target accepts
        the source-signed receipt and releases its matching target reservation.
        """

        confirmation = await self._deliver_activation_confirmation(
            awaiting, event, event_body, e2
        )
        if confirmation is None:
            return None
        target_receipt = await self._target_receipt_from_activation_confirmation(
            awaiting, confirmation, base=awaiting.base_epoch
        )
        if target_receipt is None:
            return None
        await self.store.put_activation_fence(
            awaiting.space_id,
            awaiting.pair_id,
            now_ms=self._clock_ms(),
            phase="source_terminal_confirmation",
        )
        active = awaiting.transition(MeshPairingState.ACTIVE, now_ms=self._clock_ms())
        await self.store.put_session(active)
        terminal_confirmed = await self._deliver_source_terminal_confirmation(
            active, event, target_receipt=target_receipt
        )
        if terminal_confirmed:
            await self.store.release_activation_fence(active.space_id, active.pair_id)
            await self.store.release_source_activation_marker(
                active.space_id, active.pair_id
            )
        return active, terminal_confirmed

    async def _reconfirm_active_source_locked(
        self, session: MeshPairingSession
    ) -> bool:
        """Re-prove an ACTIVE source receipt before accepting a duplicate ACK.

        The caller already holds ``pair_lock``.  This mirrors the source-side
        local linearization used by resume, but deliberately does not mutate
        membership: it only replays the exact e+2 event after proving the
        source's own active membership and signed target binding.  The
        activation fence is released only after the target signs the matching
        confirmation.
        """

        if session.role != MeshPairingRole.SOURCE.value:
            return False
        try:
            approval = await self._source_enrollment_approval(session)
            if (
                session.target_public_key != approval.target_public_key
                or session.target_fingerprint != approval.target_fingerprint
            ):
                return False
            target_node_id = _node_id_from_fingerprint(approval.target_fingerprint)
            e2 = session.base_epoch + 2
            store = self._hive_store(session.space_id)
            async with self.store.space_lock(session.space_id):
                membership_svc = self._membership(session.space_id)
                async with membership_svc.space_lock(), token_mutation_lock(
                    session.space_id
                ):
                    fresh = await self.store.get_session(session.pair_id)
                    if (
                        fresh is None
                        or fresh.role != MeshPairingRole.SOURCE.value
                        or fresh.state != MeshPairingState.ACTIVE.value
                        or fresh.target_public_key != approval.target_public_key
                        or fresh.target_fingerprint != approval.target_fingerprint
                        or not await self._source_is_healthy_for_bootstrap(fresh)
                    ):
                        return False
                    membership, node, term = await asyncio.gather(
                        store.get_membership(),
                        store.get_node_identity(),
                        store.get_term(),
                    )
                    if (
                        membership is None
                        or node is None
                        or term is None
                        or membership.epoch != e2
                    ):
                        return False
                    target_member = next(
                        (
                            member
                            for member in membership.members
                            if member.node_id == target_node_id
                        ),
                        None,
                    )
                    if (
                        target_member is None
                        or target_member.status != MemberStatus.ACTIVE.value
                    ):
                        return False
                    digest = candidate_view_digest(membership)
                    event = EventEnvelope(
                        event_id=(
                            fresh.activation_event_id
                            or _membership_event_id(
                                fresh.space_id, target_node_id, e2
                            )
                        ),
                        request_id=self._request_id_factory(),
                        type=EventType.MEMBERSHIP_UPDATED,
                        origin_node_id=node.node_id,
                        term=term.term,
                        membership_epoch=e2,
                        payload={
                            "node_id": target_node_id,
                            "epoch": e2,
                            "status": MemberStatus.ACTIVE.value,
                            "candidate_view_digest": digest,
                            "pair_id": fresh.pair_id,
                        },
                    )
                    event_body = canonical_dumps(event.model_dump(mode="json"))
            fence = await self.store.get_activation_fence_record(fresh.space_id)
            if fence is None:
                # A mutable/removed fence cannot substitute for the target's
                # signed readback.  New-protocol sources re-arm their terminal
                # fence before attempting recovery; historical sessions that
                # predate source-bootstrap evidence retain their compatibility
                # behavior.
                if await self._source_terminal_confirmation_matches(fresh):
                    return await self._replay_source_terminal_confirmation(
                        fresh, event
                    )
                if not await self._is_new_source_terminal_tail(fresh):
                    return True
                await self.store.put_activation_fence(
                    fresh.space_id,
                    fresh.pair_id,
                    now_ms=self._clock_ms(),
                    phase="source_terminal_confirmation",
                )
                fence = await self.store.get_activation_fence_record(fresh.space_id)
            if fence is None or fence[0] != fresh.pair_id:
                return False

            # First ask the target for any already-finalized terminal chain.
            # This restores exact signed bytes after either local source record
            # was lost and keeps working after normal BANK_COMMIT advancement.
            # A pre-terminal target response simply falls through to the
            # original strict e+1 final-ACK path below.
            confirmation = await self._deliver_activation_confirmation(
                fresh, event, event_body, e2
            )
            if (
                confirmation is not None
                and await self._restore_source_terminal_confirmation_from_target_response(
                    fresh, event, confirmation
                )
            ):
                await self.store.release_activation_fence(
                    fresh.space_id, fresh.pair_id
                )
                await self.store.release_source_activation_marker(
                    fresh.space_id, fresh.pair_id
                )
                return True

            # A target response is needed only to obtain the digest when a
            # process crashed after source ACTIVE but before it wrote its own
            # terminal receipt.  Once the receipt exists, replay exactly that
            # signed confirmation; the target recognizes it even after later
            # legitimate BANK_COMMITs.
            signed = await self.store.get_source_activation_receipt(fresh.pair_id)
            if signed is None:
                if confirmation is None:
                    return False
                target_receipt = await self._target_receipt_from_activation_confirmation(
                    fresh, confirmation, base=fresh.base_epoch
                )
                if target_receipt is None:
                    return False
            else:
                signed.verify(self._config.public_key)
                target_receipt = SignedTargetActivationReceipt.from_dict(
                    signed.receipt.target_activation_receipt
                )
                target_receipt.verify(approval.target_public_key)
            await self.store.put_activation_fence(
                fresh.space_id,
                fresh.pair_id,
                now_ms=self._clock_ms(),
                phase="source_terminal_confirmation",
            )
            if not await self._deliver_source_terminal_confirmation(
                fresh, event, target_receipt=target_receipt
            ):
                return False
            await self.store.release_activation_fence(fresh.space_id, fresh.pair_id)
            await self.store.release_source_activation_marker(
                fresh.space_id, fresh.pair_id
            )
            return True
        except Exception:
            return False

    async def _resume_source_locked(self, session: MeshPairingSession) -> dict:
        """Resume a source activation while the caller holds ``pair_lock``.

        The final ACK retry path uses this directly.  Keeping its complete
        revalidation/promotion sequence here avoids re-entering the non-reentrant
        pair lock through the public ``resume`` operation.
        """

        pair_id = session.pair_id
        if session.role != MeshPairingRole.SOURCE.value:
            raise MeshPairingServiceError("unknown_pair", "unknown source pairing")
        if session.state == MeshPairingState.ACTIVE.value:
            # ``ACTIVE`` plus a retained activation fence is the new durable
            # source-terminal crash tail.  Re-drive its signed confirmation
            # rather than pretending that the mutable workflow state alone
            # completed all-ACK convergence.
            if await self._reconfirm_active_source_locked(session):
                return {
                    "pair_id": pair_id,
                    "state": MeshPairingState.ACTIVE.value,
                    "epoch": session.base_epoch + 2,
                }
            return {
                "pair_id": pair_id,
                "state": MeshPairingState.ACTIVE.value,
                "source_confirmation_pending": True,
            }
        if session.state not in (
            MeshPairingState.BLOCKED_RECOVERY.value,
            MeshPairingState.AWAITING_ACKS.value,
        ):
            raise MeshPairingServiceError("not_blocked", "pairing is not recoverable")
        if session.state == MeshPairingState.BLOCKED_RECOVERY.value:
            signed_ev = await self._verified_blocked_evidence(session)
            if signed_ev.evidence.next_action != "resume":
                raise MeshPairingServiceError(
                    "not_resumable", "blocked recovery is not resumable; use evict"
                )
        approval = await self._source_enrollment_approval(session)
        if (
            session.target_public_key != approval.target_public_key
            or session.target_fingerprint != approval.target_fingerprint
        ):
            raise MeshPairingServiceError(
                "source_snapshot_changed", "source target authority changed"
            )
        store = self._hive_store(session.space_id)
        target_node_id = _node_id_from_fingerprint(approval.target_fingerprint)
        e2 = session.base_epoch + 2
        # Pair -> store space -> membership -> token is the same lock order as
        # the final ACK.  No source mutation may interleave evidence validation
        # with an e+1 -> e+2 promotion.
        async with self.store.space_lock(session.space_id):
            membership_svc = self._membership(session.space_id)
            async with membership_svc.space_lock(), token_mutation_lock(session.space_id):
                fresh = await self.store.get_session(pair_id)
                if (
                    fresh is None
                    or fresh.role != MeshPairingRole.SOURCE.value
                    or fresh.state
                    not in (
                        MeshPairingState.BLOCKED_RECOVERY.value,
                        MeshPairingState.AWAITING_ACKS.value,
                    )
                ):
                    raise MeshPairingServiceError(
                        "not_blocked", "pairing is not recoverable"
                    )
                session = fresh
                if (
                    session.target_public_key != approval.target_public_key
                    or session.target_fingerprint != approval.target_fingerprint
                ):
                    await self._block_recovery(
                        session,
                        phase="bootstrap_source_changed",
                        next_action="evict",
                        manifest_digest=session.bootstrap_manifest_digest,
                    )
                    raise MeshPairingServiceError(
                        "source_snapshot_changed", "source target authority changed"
                    )
                if not await self._source_is_healthy_for_bootstrap(session):
                    raise MeshPairingServiceError(
                        "source_unavailable",
                        "source is not healthy for activation",
                    )
                membership, node, term = await asyncio.gather(
                    store.get_membership(),
                    store.get_node_identity(),
                    store.get_term(),
                )
                if (
                    membership is None
                    or node is None
                    or term is None
                    or membership.epoch not in (session.base_epoch + 1, e2)
                ):
                    raise MeshPairingServiceError(
                        "unrecoverable_epoch", "pairing epoch is not recoverable"
                    )
                existing_fence = await self.store.get_activation_fence_record(
                    session.space_id
                )
                if existing_fence is not None and existing_fence[0] != pair_id:
                    raise MeshPairingServiceError(
                        "pairing_in_flight",
                        "another Mesh activation fence owns this source",
                    )
                await self.store.put_activation_fence(
                    session.space_id,
                    pair_id,
                    now_ms=self._clock_ms(),
                    phase=(
                        "activation"
                        if existing_fence is None
                        else existing_fence[1]
                    ),
                )
                if membership.epoch == e2:
                    promoted = next(
                        (
                            member
                            for member in membership.members
                            if member.node_id == target_node_id
                        ),
                        None,
                    )
                    if promoted is None or promoted.status != MemberStatus.ACTIVE.value:
                        raise MeshPairingServiceError(
                            "unrecoverable_epoch",
                            "epoch advanced to e+2 without this pairing's promotion",
                        )
                else:
                    if not await self._source_bootstrap_evidence_matches(session):
                        await self._block_recovery(
                            session,
                            phase="bootstrap_source_changed",
                            next_action="evict",
                            manifest_digest=session.bootstrap_manifest_digest,
                        )
                        raise MeshPairingServiceError(
                            "source_snapshot_changed",
                            "source snapshot changed; pairing is evict-only",
                        )
                    pending = next(
                        (
                            member
                            for member in membership.members
                            if member.node_id == target_node_id
                        ),
                        None,
                    )
                    if pending is None or pending.status != MemberStatus.PENDING.value:
                        raise MeshPairingServiceError(
                            "unrecoverable_epoch",
                            "target is not pending at the resumable epoch",
                        )
                    try:
                        membership = await membership_svc.promote_pending_to_active_locked(
                            target_node_id,
                            expected_epoch=session.base_epoch + 1,
                            activation_pair_id=session.pair_id,
                        )
                    except MembershipEpochError as exc:
                        raise MeshPairingServiceError(
                            "unrecoverable_epoch",
                            "membership epoch changed during resume",
                        ) from exc
                    except PairingActivationError as exc:
                        raise MeshPairingServiceError(
                            "pairing_in_flight",
                            "another Mesh pairing for this space is mid-activation",
                        ) from exc
                digest = candidate_view_digest(membership)
                event = EventEnvelope(
                    event_id=(
                        session.activation_event_id
                        or _membership_event_id(session.space_id, target_node_id, e2)
                    ),
                    request_id=self._request_id_factory(),
                    type=EventType.MEMBERSHIP_UPDATED,
                    origin_node_id=node.node_id,
                    term=term.term,
                    membership_epoch=e2,
                    payload={
                        "node_id": target_node_id,
                        "epoch": e2,
                        "status": MemberStatus.ACTIVE.value,
                        "candidate_view_digest": digest,
                        "pair_id": session.pair_id,
                    },
                )
                body = canonical_dumps(event.model_dump(mode="json"))
        completed = await self._complete_source_activation_tail(
            session, event, body, e2=e2
        )
        if completed is None:
            if session.state != MeshPairingState.BLOCKED_RECOVERY.value:
                await self._block_recovery(
                    session,
                    phase="activation_unconfirmed",
                    next_action="resume",
                    candidate_view_digest=digest,
                    activation_event_id=event.event_id,
                )
            return {
                "pair_id": pair_id,
                "state": MeshPairingState.BLOCKED_RECOVERY.value,
            }
        active, source_terminal_confirmed = completed
        if not source_terminal_confirmed:
            return {
                "pair_id": pair_id,
                "state": active.state,
                "source_confirmation_pending": True,
            }
        return {"pair_id": pair_id, "state": active.state, "epoch": e2}

    async def resume(self, pair_id: str) -> dict:
        """Resume a source pairing stranded in ``blocked_recovery`` by idempotently
        re-delivering the e+2 activation to the target (no rollback, no re-admit).

        Usually membership is already at e+2 and this re-drives only delivery.
        A crash can instead leave durable ``awaiting_acks`` intent before the
        e+1 -> e+2 write; that path revalidates the immutable source bootstrap
        binding in the same local critical section as promotion.
        """

        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.SOURCE.value:
                raise MeshPairingServiceError("unknown_pair", "unknown source pairing")
            return await self._resume_source_locked(session)


    async def cancel(self, pair_id: str) -> dict:
        """Cancel a PRE-MUTATION pairing (issued/claimed/approved): releases the
        target reservation and leaves membership unchanged (PROJECT_MESH.md §7).
        A pairing past the shared-mutation boundary must use ``evict`` instead."""

        session = await self.store.get_session(pair_id)
        if session is None:
            raise MeshPairingServiceError("unknown_pair", "unknown pairing")
        if session.role == MeshPairingRole.SOURCE.value:
            # A barrier can survive a crash before the source mutable session
            # reaches CANCELLED.  Treat it as the abort authority before
            # consulting the operational state: otherwise a replayed CLAIMED
            # session can make cancel derive a target identity from empty or
            # stale fields, while approve remains correctly blocked forever.
            barrier = await self.store.get_source_preclaim_cancel_barrier(pair_id)
            if barrier is not None:
                async with self.store.pair_lock(pair_id):
                    fresh = await self.store.get_session(pair_id)
                    if (
                        fresh is None
                        or fresh.role != MeshPairingRole.SOURCE.value
                    ):
                        raise MeshPairingServiceError(
                            "not_cancellable", "pairing is no longer cancellable"
                        )
                    async with self.store.space_lock(fresh.space_id):
                        membership_svc = self._membership(fresh.space_id)
                        async with membership_svc.space_lock(), token_mutation_lock(
                            fresh.space_id
                        ):
                            current = await self.store.get_session(pair_id)
                            if (
                                current is None
                                or current.role != MeshPairingRole.SOURCE.value
                                or current.state
                                not in (
                                    MeshPairingState.ISSUED.value,
                                    MeshPairingState.CLAIMED.value,
                                    MeshPairingState.APPROVED.value,
                                    MeshPairingState.CANCELLED.value,
                                )
                            ):
                                raise MeshPairingServiceError(
                                    "not_cancellable",
                                    "pairing is no longer cancellable",
                                )
                            membership = await self._hive_store(
                                current.space_id
                            ).get_membership()
                            if (
                                membership is None
                                or not await self._source_preclaim_cancel_barrier_matches(
                                    barrier, current, membership
                                )
                            ):
                                raise MeshPairingServiceError(
                                    "cancel_unproven",
                                    "source pre-claim cancellation barrier is invalid",
                                )
                            disposition = (
                                await self.store.get_source_terminal_disposition(
                                    pair_id
                                )
                            )
                            if disposition is not None:
                                if (
                                    current.state
                                    != MeshPairingState.CANCELLED.value
                                    or not await self._source_terminal_disposition_matches_session(
                                        disposition, current
                                    )
                                    or disposition.receipt.disposition
                                    != "pre_t1_cancel"
                                ):
                                    raise MeshPairingServiceError(
                                        "terminal_disposition_unavailable",
                                        "source terminal disposition is invalid",
                                    )
                            elif (
                                current.target_public_key
                                and current.target_fingerprint
                                and current.target_endpoint
                                and current.claim_digest
                            ):
                                # Crash recovery after the late claim bound the
                                # source session but before it wrote the
                                # target-facing receipt.  Build it only from
                                # the retained signed invitation/claim chain
                                # *and* the durable one-time-secret burn.  The
                                # operational session/blob alone are mutable
                                # and cannot prove that the target actually
                                # presented the invitation secret.
                                try:
                                    if not await self._source_invitation_secret_matches_session(
                                        current
                                    ):
                                        raise MeshPairingStoreError(
                                            "secret_conflict",
                                            "source invitation secret does not match session",
                                        )
                                    secret_burned = await self.store.is_secret_burned(
                                        pair_id,
                                        secret_digest=current.secret_digest,
                                    )
                                except MeshPairingStoreError as exc:
                                    raise MeshPairingServiceError(
                                        "cancel_unproven",
                                        "late claim has no durable secret-consumption proof",
                                    ) from exc
                                if not secret_burned:
                                    raise MeshPairingServiceError(
                                        "cancel_unproven",
                                        "late claim has no durable secret-consumption proof",
                                    )
                                await self._persist_source_terminal_disposition(
                                    current,
                                    disposition="pre_t1_cancel",
                                    membership=membership,
                                )
                            else:
                                updates = self._preclaim_cancelled_session_updates()
                                if current.state == MeshPairingState.CANCELLED.value:
                                    cancelled = current.with_fields(
                                        now_ms=self._clock_ms(), **updates
                                    )
                                else:
                                    cancelled = current.transition(
                                        MeshPairingState.CANCELLED,
                                        now_ms=self._clock_ms(),
                                        **updates,
                                    )
                                await self.store.put_session(cancelled)
                return {"pair_id": pair_id, "state": MeshPairingState.CANCELLED.value}
        if (
            session.role == MeshPairingRole.TARGET.value
            and session.state == MeshPairingState.CANCELLED.value
        ):
            # A crash can persist the terminal local session after the signed
            # target-fence release but before raw-reservation deletion.  The
            # signed released tail — not the mutable terminal session — is the
            # proof that this was a genuine pre-mutation cancellation.
            async with self.store.pair_lock(pair_id):
                fresh = await self.store.get_session(pair_id)
                if (
                    fresh is None
                    or fresh.role != MeshPairingRole.TARGET.value
                    or fresh.state != MeshPairingState.CANCELLED.value
                ):
                    raise MeshPairingServiceError(
                        "not_cancellable", "pairing is no longer cancellable"
                    )
                async with self.store.space_lock(fresh.space_id):
                    release_proof = await self._target_pre_mutation_release_matches(fresh)
                    if release_proof is False:
                        raise MeshPairingServiceError(
                            "not_cancellable",
                            "target cancellation lacks signed pre-mutation release proof",
                        )
                    reservation = await self.store.get_reservation_direct(fresh.space_id)
                    if reservation not in (None, pair_id):
                        raise MeshPairingServiceError(
                            "not_cancellable", "target reservation belongs to another pairing"
                        )
                    if release_proof is None and reservation == pair_id:
                        # Old records have no direct #417 proof.  Preserve the
                        # legacy crash retry only when the target is still
                        # blank; never infer that from a mutable CANCELLED
                        # session in a populated target.
                        try:
                            await self._bootstrap()._assert_blank_target(fresh.space_id)
                        except BootstrapError as exc:
                            raise MeshPairingServiceError(
                                "not_cancellable", "legacy target cancellation is not blank"
                            ) from exc
                    await self.store.release(fresh.space_id, pair_id)
            return {"pair_id": pair_id, "state": MeshPairingState.CANCELLED.value}
        if session.role == MeshPairingRole.TARGET.value:
            # A TARGET session is local operational state and can be rewritten
            # from a post-T1 import tail back to CLAIMED/APPROVED.  It must not
            # independently authorize releasing the target reservation: the
            # source might already have admitted the PENDING candidate.  The
            # only safe target-side exit is the same signed source-terminal
            # proof used by abandon(), which additionally checks the source
            # membership before reporting CANCELLED/REFUSED/EXPIRED as usable.
            return await self.abandon(pair_id)
        if session.state_enum not in PRE_MUTATION_STATES:
            raise MeshPairingServiceError(
                "not_cancellable", "pairing passed the shared-mutation boundary; use evict"
            )
        async with self.store.pair_lock(pair_id):
            fresh = await self.store.get_session(pair_id)
            if (
                fresh is None
                or fresh.role != MeshPairingRole.SOURCE.value
                or fresh.state_enum not in PRE_MUTATION_STATES
            ):
                raise MeshPairingServiceError("not_cancellable", "pairing is no longer cancellable")
            # Linearize source cancellation with approval/admission.  Reading
            # membership before these locks lets approval admit e+1 in the gap,
            # after which a stale cancel can overwrite its source session to
            # CANCELLED and strand the PENDING target.  Match approve's order:
            # pair -> store-space -> membership -> token.
            async with self.store.space_lock(fresh.space_id):
                membership_svc = self._membership(fresh.space_id)
                async with membership_svc.space_lock(), token_mutation_lock(
                    fresh.space_id
                ):
                    current = await self.store.get_session(pair_id)
                    if (
                        current is None
                        or current.role != MeshPairingRole.SOURCE.value
                        or current.state_enum not in PRE_MUTATION_STATES
                    ):
                        raise MeshPairingServiceError(
                            "not_cancellable", "pairing is no longer cancellable"
                        )
                    membership = await self._hive_store(current.space_id).get_membership()
                    if membership is None:
                        raise MeshPairingServiceError(
                            "membership_unavailable",
                            "membership state is unavailable; cannot cancel pairing",
                        )
                    if current.state == MeshPairingState.ISSUED.value:
                        # Before a claim the source does not know the target
                        # identity.  Persist a source-only abort barrier first
                        # so a claim already in transit can be bound exactly
                        # once to the ordinary target-facing disposition rather
                        # than stranding its local reservation.
                        await self._persist_source_preclaim_cancel_barrier(
                            current, membership
                        )
                    else:
                        target_node_id = _node_id_from_fingerprint(
                            current.target_fingerprint
                        )
                        if any(
                            member.node_id == target_node_id
                            and member.status
                            in (MemberStatus.PENDING.value, MemberStatus.ACTIVE.value)
                            for member in membership.members
                        ):
                            raise MeshPairingServiceError(
                                "already_admitted", "target already admitted; use evict"
                            )
                        # Durable before the mutable terminal session: the
                        # accepted target can later verify this exact
                        # invitation/claim-bound source proof, including if
                        # this process crashes immediately after the write.  An
                        # unclaimed invitation has no target reservation or
                        # claim root to release, so it intentionally carries no
                        # target-facing disposition.
                        await self._persist_source_terminal_disposition(
                            current,
                            disposition="pre_t1_cancel",
                            membership=membership,
                        )
                    cancelled = current.transition(
                        MeshPairingState.CANCELLED, now_ms=self._clock_ms()
                    )
                    await self.store.put_session(cancelled)
        return {"pair_id": pair_id, "state": MeshPairingState.CANCELLED.value}

    async def evict(self, pair_id: str, *, operator: str, reason: str = "") -> dict:
        """Operator give-up: remove the admitted PENDING candidate of a FAILED source
        pairing through the existing membership authority (epoch-advancing, audited)
        and release the reservation. Covers a ``blocked_recovery`` pairing that
        cannot converge, the crash-window ``approved`` session whose target is
        admitted, and a stuck ``transferring``/``awaiting_acks`` pairing whose target
        is unresponsive — so an admitted candidate is never un-removable
        (PROJECT_MESH.md §7).

        It REFUSES a target that is already ACTIVE in shared membership: Transition 2
        committed, so the target may be a live active member whose e+2 activation
        applied while only the confirmation was lost. Unilaterally removing it would
        split membership (source drops it, target still serves active) with no target
        recovery. That case is convergence-only — ``resume`` idempotently re-drives
        the activation and converges both sides; a genuinely dead active node is
        removed via ordinary membership eviction, not pairing give-up."""

        if not operator:
            raise MeshPairingServiceError("operator_required", "eviction requires an operator")
        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.SOURCE.value:
                raise MeshPairingServiceError("unknown_pair", "unknown source pairing")
            target_node_id = (
                _node_id_from_fingerprint(session.target_fingerprint) if session.target_fingerprint else ""
            )
            store = self._hive_store(session.space_id)
            membership = await store.get_membership()
            if membership is None:
                raise MeshPairingServiceError(
                    "membership_unavailable",
                    "membership state is unavailable; cannot evict pairing",
                )
            if session.state == MeshPairingState.CANCELLED.value:
                # A hard crash after persisting CANCELLED but before releasing
                # the signed per-space marker leaves an otherwise terminal
                # post-eviction tail.  No membership mutation is retried here:
                # validate that the marker is bound to the adjacent EVICTED
                # (or absent) tail, then finish only the idempotent local
                # cleanup.  Any mismatched marker remains fail-closed.
                marker = await self.store.get_source_activation_marker(
                    session.space_id
                )
                if marker is not None:
                    if not await self._source_activation_marker_matches_current_tail(
                        marker, session, membership
                    ):
                        raise MeshPairingServiceError(
                            "not_evictable",
                            "cancelled pairing marker does not match current membership",
                        )
                    await self.store.release_source_activation_marker(
                        session.space_id, pair_id
                    )
                await self.store.release_activation_fence(session.space_id, pair_id)
                return {
                    "pair_id": pair_id,
                    "state": MeshPairingState.CANCELLED.value,
                    "evicted_node": target_node_id,
                }
            # Couple the signed pre-removal intent and the PENDING -> EVICTED
            # write under the same source authority locks.  A source session
            # and a MembershipView are operational records; neither may be
            # used after the fact as an oracle for a new target-release proof.
            async with self.store.space_lock(session.space_id):
                membership_svc = self._membership(session.space_id)
                async with membership_svc.space_lock(), token_mutation_lock(
                    session.space_id
                ):
                    fresh = await self.store.get_session(pair_id)
                    membership = await store.get_membership()
                    if (
                        fresh is None
                        or fresh.role != MeshPairingRole.SOURCE.value
                        or membership is None
                    ):
                        raise MeshPairingServiceError(
                            "membership_unavailable",
                            "pairing or membership state changed during eviction",
                        )
                    session = fresh
                    target_node_id = _node_id_from_fingerprint(
                        session.target_fingerprint
                    )
                    target_key = _legacy_membership_key(session.target_public_key)
                    member = next(
                        (
                            item
                            for item in membership.members
                            if item.node_id == target_node_id
                            and item.public_key == target_key
                        ),
                        None,
                    )
                    if member is not None and member.status == MemberStatus.ACTIVE.value:
                        raise MeshPairingServiceError(
                            "target_active",
                            "target is active in shared membership; resume to converge (do not evict a promoted member)",
                        )
                    pending_intent = await self.store.get_source_pending_eviction_intent(
                        pair_id
                    )
                    # A crash can occur after the source-authorized PENDING
                    # removal and before its terminal disposition/session
                    # write.  The source session can still be APPROVED in
                    # that prefix, so recognize this *only* from the signed
                    # pre-removal intent plus the adjacent EVICTED view.  Do
                    # not let an arbitrary mutable EVICTED record bypass the
                    # normal state gate.
                    pending_eviction_retry = (
                        pending_intent is not None
                        and session.state
                        in (
                            MeshPairingState.APPROVED.value,
                            MeshPairingState.TRANSFERRING.value,
                            MeshPairingState.AWAITING_ACKS.value,
                            MeshPairingState.BLOCKED_RECOVERY.value,
                        )
                        and await self._source_pending_eviction_intent_matches_session(
                            pending_intent, session
                        )
                        and membership.epoch == session.base_epoch + 2
                        and member is not None
                        and member.status == MemberStatus.EVICTED.value
                        and member.incarnation == session.pair_id
                    )
                    admitted_pending = (
                        member is not None
                        and member.status == MemberStatus.PENDING.value
                    )
                    if admitted_pending and member.incarnation != session.pair_id:
                        raise MeshPairingServiceError(
                            "stale_pairing",
                            "target pending candidate does not belong to this pairing",
                        )
                    if (
                        not pending_eviction_retry
                        and session.state != MeshPairingState.BLOCKED_RECOVERY.value
                        and not (
                            admitted_pending
                            and session.state
                            in (
                                MeshPairingState.APPROVED.value,
                                MeshPairingState.TRANSFERRING.value,
                                MeshPairingState.AWAITING_ACKS.value,
                            )
                        )
                    ):
                        raise MeshPairingServiceError(
                            "not_evictable",
                            "only a blocked or dangling pairing may be evicted",
                        )

                    if admitted_pending:
                        if pending_intent is None:
                            pending_intent = await self._persist_source_pending_eviction_intent(
                                session, membership
                            )
                        elif not await self._source_pending_eviction_intent_matches_membership(
                            pending_intent, session, membership
                        ):
                            raise MeshPairingServiceError(
                                "eviction_unproven",
                                "source pending eviction intent does not match current membership",
                            )
                        if session.state in (
                            MeshPairingState.TRANSFERRING.value,
                            MeshPairingState.AWAITING_ACKS.value,
                        ):
                            session = await self._block_recovery(
                                session,
                                phase="operator_abandoned",
                                next_action="evict",
                            )
                        try:
                            disposition_membership = await membership_svc._remove_pending_candidate_locked(
                                target_node_id,
                                operator=operator,
                                reason=reason,
                                expected_incarnation=session.pair_id,
                                activation_pair_id=session.pair_id,
                            )
                        except MembershipIncarnationError as exc:
                            raise MeshPairingServiceError(
                                "stale_pairing",
                                "target pending candidate does not belong to this pairing",
                            ) from exc
                        except PairingActivationError as exc:
                            raise MeshPairingServiceError(
                                "pairing_in_flight",
                                "another Mesh pairing for this space is mid-activation",
                            ) from exc
                    else:
                        # The only no-PENDING retry accepted is a crash after a
                        # previously persisted exact intent and the adjacent
                        # membership removal.  No intent means a mutable
                        # EVICTED rewrite cannot trigger a new source signature.
                        if not pending_eviction_retry:
                            raise MeshPairingServiceError(
                                "eviction_unproven",
                                "source pending eviction cannot be proven",
                            )
                        disposition_membership = membership

                existing_disposition = await self.store.get_source_terminal_disposition(
                    session.pair_id
                )
                if existing_disposition is None:
                    await self._persist_source_terminal_disposition(
                        session,
                        disposition="pending_evicted",
                        membership=disposition_membership,
                    )
                elif (
                    not await self._source_terminal_disposition_matches_session(
                        existing_disposition, session
                    )
                    or existing_disposition.receipt.disposition != "pending_evicted"
                ):
                    raise MeshPairingServiceError(
                        "eviction_unproven",
                        "source eviction disposition is unavailable",
                    )
                await self.store.release(session.space_id, session.pair_id)
                cancelled = session.transition(
                    MeshPairingState.CANCELLED, now_ms=self._clock_ms()
                )
                await self.store.put_session(cancelled)
                await self.store.release_activation_fence(session.space_id, pair_id)
                await self.store.release_source_activation_marker(
                    session.space_id, pair_id
                )
        return {"pair_id": pair_id, "state": MeshPairingState.CANCELLED.value, "evicted_node": target_node_id}

    async def force_evict_member(self, pair_id: str, *, operator: str, reason: str = "") -> dict:
        """Operator-forced removal of a DEAD ACTIVE target of a settled pairing.

        A promoted (ACTIVE) target may be dead in two settled situations: a pairing
        that CONVERGED normally (source session ``active``) whose member later died,
        or a pairing stuck in ``blocked_recovery`` whose promoted target never
        confirmed and is unreachable. In both, the convergence path (``resume``)
        cannot reach a dead node and pairing give-up (``evict``) correctly refuses a
        promoted member, so the dead node would otherwise stay in the all-ACK roster
        and freeze shared work. If the operator KNOWS the target is dead, this
        removes it through the ordinary epoch-advancing, audited
        ``MembershipService.evict_member`` authority.

        It is unsafe (split-brain) if the target is in fact alive, so the operator
        asserts the node is dead — the same contract as force-removing any full-mesh
        node. It is idempotent (safe to retry after a crash mid-operation): if the
        member is already gone it just settles the pairing. It REFUSES a PENDING
        target (use ``evict``) and a mid-flight pairing
        (``transferring``/``awaiting_acks`` — ``resume`` first, which durably
        persists ``blocked_recovery``)."""

        if not operator:
            raise MeshPairingServiceError("operator_required", "member eviction requires an operator")
        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.SOURCE.value:
                raise MeshPairingServiceError("unknown_pair", "unknown source pairing")
            target_node_id = (
                _node_id_from_fingerprint(session.target_fingerprint)
                if session.target_fingerprint
                else ""
            )
            store = self._hive_store(session.space_id)
            membership = await store.get_membership()
            # A force eviction can crash after its durable CANCELLED session
            # write and before it releases the marker/fence.  The same explicit
            # operator retry is required to finish that tail; rejecting it as
            # ``not_forcible`` contradicts the operation's idempotent recovery
            # contract.  This branch never retries an epoch mutation: it accepts
            # only the retained EVICTED incarnation and an exactly adjacent
            # signed marker (when one remains), then clears local cleanup state.
            if session.state == MeshPairingState.CANCELLED.value:
                if membership is None:
                    raise MeshPairingServiceError(
                        "membership_unavailable",
                        "membership state is unavailable; cannot finish force-eviction cleanup",
                    )
                member = next(
                    (
                        item
                        for item in membership.members
                        if item.node_id == target_node_id
                    ),
                    None,
                )
                if (
                    member is None
                    or member.status != MemberStatus.EVICTED.value
                    or member.incarnation != session.pair_id
                ):
                    raise MeshPairingServiceError(
                        "not_forcible",
                        "cancelled pairing is not the retained force-eviction tail",
                    )
                marker = await self.store.get_source_activation_marker(
                    session.space_id
                )
                if marker is not None:
                    if not await self._source_activation_marker_matches_current_tail(
                        marker, session, membership
                    ):
                        raise MeshPairingServiceError(
                            "not_forcible",
                            "cancelled pairing marker does not match current membership",
                        )
                    await self.store.release_source_activation_marker(
                        session.space_id, pair_id
                    )
                await self.store.release_activation_fence(session.space_id, pair_id)
                return {
                    "pair_id": pair_id,
                    "state": MeshPairingState.CANCELLED.value,
                    "evicted_node": target_node_id,
                }
            # Only a SETTLED pairing: a converged 'active' session, or a blocked one.
            # A mid-flight pairing must resume first (which durably persists
            # blocked_recovery when it cannot converge).
            if session.state not in (
                MeshPairingState.ACTIVE.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
            ):
                raise MeshPairingServiceError(
                    "not_forcible",
                    "only a converged or blocked pairing's dead active member is force-evictable; resume/evict first",
                )
            # A blocked pairing must carry verifiable signed evidence before we act;
            # a converged 'active' session is itself the durable proof of Transition 2.
            if session.state == MeshPairingState.BLOCKED_RECOVERY.value:
                await self._verified_blocked_evidence(session)
            # Fail closed on missing critical membership: an active/blocked source
            # session proves an e+2 membership existed, and evict_member PRESERVES an
            # EVICTED record — so a missing members.json (or the member record gone)
            # is state loss/corruption, NOT an idempotent retry. Never falsely report
            # recovery over lost membership.
            if membership is None:
                raise MeshPairingServiceError(
                    "membership_unavailable", "membership state is unavailable; cannot force-evict"
                )
            member = next((m for m in membership.members if m.node_id == target_node_id), None)
            if member is None:
                raise MeshPairingServiceError(
                    "membership_unavailable", "target member record is missing; cannot force-evict"
                )
            if member.status == MemberStatus.PENDING.value:
                raise MeshPairingServiceError(
                    "not_active", "target is a pending candidate; use evict, not force member-eviction"
                )
            if member.status == MemberStatus.ACTIVE.value:
                # Incarnation binding, ATOMIC (compare-and-evict under the membership
                # lock): only the incarnation THIS pairing admitted (tagged with this
                # pair_id) may be force-evicted. A re-admission of the same identity
                # carries a fresh pair_id, so evict_member fails closed
                # (MembershipIncarnationError) rather than removing the newer live
                # member — and, being inside the lock, a concurrent re-admit cannot
                # race between this check and the eviction write. (This is stable
                # under an unrelated rescope, which changes the epoch but not the
                # incarnation.)
                try:
                    await self._membership(session.space_id).evict_member(
                        target_node_id,
                        operator=operator,
                        reason=reason,
                        confirm=True,
                        expected_incarnation=session.pair_id,
                        activation_pair_id=session.pair_id,
                    )
                except MembershipIncarnationError as exc:
                    raise MeshPairingServiceError(
                        "stale_pairing",
                        "target has been re-admitted since this pairing; refusing to evict a newer incarnation",
                    ) from exc
                except PairingActivationError as exc:
                    # Defense-in-depth: force-evict bypasses its OWN session and the
                    # single-in-flight gate precludes a different mid-activation
                    # pairing; fail structured rather than 500 if one existed.
                    raise MeshPairingServiceError(
                        "pairing_in_flight",
                        "another Mesh pairing for this space is mid-activation",
                    ) from exc
            # else: a retained EVICTED/LEAVING record is an idempotent no-op (this or
            # a prior eviction already removed it); settle the pairing below.
            await self.store.release(session.space_id, session.pair_id)
            # A blocked pairing reconciles to cancelled; a converged 'active' session
            # is already terminal and stays as the historical record of the
            # (successful) pairing whose member was later removed.
            state = session.state
            if session.state == MeshPairingState.BLOCKED_RECOVERY.value:
                cancelled = session.transition(MeshPairingState.CANCELLED, now_ms=self._clock_ms())
                await self.store.put_session(cancelled)
                state = cancelled.state
            await self.store.release_activation_fence(session.space_id, pair_id)
            await self.store.release_source_activation_marker(
                session.space_id, pair_id
            )
        return {"pair_id": pair_id, "state": state, "evicted_node": target_node_id}

    # ==================================================================
    # TARGET — inbound event (confined self-activation, router branch)
    # ==================================================================

    async def try_pending_self_activation(
        self, envelope: MeshRequestEnvelope, event
    ) -> Optional[HandlerResult]:
        """Confined pending-self-activation, called ONLY inside the router's
        ``except _LocalUnsafe:`` branch. Returns a signed OK on the exact
        session-bound e+1->e+2 self-promotion, else ``None`` (router re-emits the
        byte-identical LOCAL_UNSAFE for every other event)."""

        # (a) a local TARGET session for this space awaiting activation
        payload = event.payload if isinstance(event.payload, dict) else {}
        session = await self._find_target_session(
            envelope.space_id,
            envelope.source_fingerprint,
            pair_id=payload.get("pair_id", ""),
        )
        if session is None:
            return None
        base = session.base_epoch
        # (b) the event promotes THIS node pending->active at exactly e+2
        if (
            getattr(event, "type", None) != EventType.MEMBERSHIP_UPDATED.value
            or event.membership_epoch != base + 2
        ):
            return None
        store = self._hive_store(session.space_id)
        node, membership, health = await asyncio.gather(
            store.get_node_identity(),
            store.get_membership(),
            store.get_node_status(),
        )
        # This narrow router escape hatch exists only for the transitional
        # bootstrap state.  A RESYNC_REQUIRED/other UNSAFE authority must never
        # be healed by replaying an old source activation event.
        authority_lost = (
            health is not None
            and health.reason == "mesh_activation_authority_lost"
        )
        if (
            node is None
            or membership is None
            or health is None
            or health.status != HiveNodeStatus.UNSAFE.value
            or health.reason
            not in {"mesh_pending_activation", "mesh_activation_authority_lost"}
        ):
            return None
        self_id = node.node_id
        if (
            payload.get("node_id") != self_id
            or payload.get("status") != MemberStatus.ACTIVE.value
            or payload.get("epoch") != base + 2
        ):
            return None
        # (f) idempotent: membership already applied e+2 (self ACTIVE). A crash may
        # have promoted membership but not yet finished the finalize tail (HEALTHY
        # + reservation release + session -> active), so RE-RUN that tail here
        # rather than short-circuit on membership alone — otherwise a target that
        # crashed before flipping HEALTHY would stay UNSAFE/reserved forever while
        # the source (which sees the re-delivered confirmation) goes active.
        if membership.epoch == base + 2:
            self_member = next((m for m in membership.members if m.node_id == self_id), None)
            if self_member is not None and self_member.status == MemberStatus.ACTIVE.value:
                if (
                    not _source_event_is_eligible(
                        membership, envelope.source_public_key, event.origin_node_id
                    )
                    or payload.get("candidate_view_digest")
                    != candidate_view_digest(membership)
                ):
                    return None
                signed_source: SignedSourceActivationReceipt | None = None
                if "source_activation_receipt" in payload:
                    signed_source = await self._source_activation_receipt_for_event(
                        session, envelope, event, base=base
                    )
                    if signed_source is None or not await self._restore_target_activation_receipt_from_source_receipt(
                        session, base=base, signed_source=signed_source
                    ):
                        return _refuse(MeshResponseCode.LOCAL_UNSAFE)
                if authority_lost:
                    terminal = (
                        None
                        if signed_source is None
                        else await self._target_terminal_confirmation_for_event(
                            session, signed_source, event, base=base
                        )
                    )
                    if terminal is not None:
                        if not await self._restore_target_final_confirmation_from_source_receipt(
                            session,
                            signed_source,
                            base=base,
                            signed_terminal=terminal,
                            allow_reserved_recovery=True,
                        ):
                            return _refuse(MeshResponseCode.LOCAL_UNSAFE)
                    elif not await self._restore_target_terminal_receipt_from_event(
                        session, envelope, event, base=base
                    ):
                        # A source receipt without its terminal confirmation is
                        # a legitimate first-tail message.  It still needs the
                        # strict e+1 proof and may not repair a post-commit
                        # authority-loss fence by itself.
                        return _refuse(MeshResponseCode.LOCAL_UNSAFE)
                if session.state == MeshPairingState.ACTIVE.value:
                    # Even an idempotent e+2 delivery must not turn a
                    # syntactically rewritten ACTIVE receipt into a healthy,
                    # unfenced target.  The durable import marker remains an
                    # activation authority through every confirmation tail.
                    completed = await self._prove_and_complete_active_target_tail(
                        session, base=base
                    )
                    if completed.code is MeshResponseCode.OK:
                        confirmed = await self._complete_target_source_terminal_confirmation(
                            session, envelope, event, base=base
                        )
                        if confirmed is not None:
                            completed = confirmed
                        await self.store.put_receipt(
                            _receipt_token(envelope.nonce),
                            {"applied": True, "epoch": base + 2},
                        )
                    return completed
                await self.store.put_receipt(
                    _receipt_token(envelope.nonce), {"applied": True, "epoch": base + 2}
                )
                return await self._finalize_target_activation_delivery(
                    session, envelope, event, base=base
                )
        # (d) exact e+1 -> e+2 and self is PENDING at e+1
        if authority_lost:
            return None
        if membership.epoch != base + 1:
            return None
        self_member = next((m for m in membership.members if m.node_id == self_id), None)
        if self_member is None or self_member.status != MemberStatus.PENDING.value:
            return None
        # (c) the source (event origin) is an eligible ACTIVE commit member of the
        # LOCAL e+1 view
        if not _source_event_is_eligible(
            membership, envelope.source_public_key, event.origin_node_id
        ):
            return None
        # (e) the source-signed candidate-view digest matches the target's own
        # recomputed e+2 view
        try:
            projected = projected_promotion_view(membership, self_id)
        except ValueError:
            return None
        if payload.get("candidate_view_digest") != candidate_view_digest(projected):
            return None

        # Close the last local TOCTOU before self-promotion. The same lock order
        # as source membership/commit mutations makes the import proof and e+2
        # membership application one local critical section.
        membership_svc = self._membership(session.space_id)
        async with membership_svc.space_lock(), token_mutation_lock(session.space_id):
            if not await self._import_validation_matches(session, base=base):
                await self._mark_target_import_validation_failure(session)
                return None
            locked_node = await store.get_node_identity()
            locked_membership = await store.get_membership()
            if locked_node is None or locked_membership is None:
                return None
            locked_self_id = locked_node.node_id
            locked_self = next(
                (
                    member
                    for member in locked_membership.members
                    if member.node_id == locked_self_id
                ),
                None,
            )
            if (
                locked_self_id != self_id
                or locked_membership.epoch != base + 1
                or locked_self is None
                or locked_self.status != MemberStatus.PENDING.value
                or not _source_event_is_eligible(
                    locked_membership, envelope.source_public_key, event.origin_node_id
                )
            ):
                return None
            try:
                locked_projected = projected_promotion_view(
                    locked_membership, locked_self_id
                )
            except ValueError:
                return None
            if payload.get("candidate_view_digest") != candidate_view_digest(
                locked_projected
            ):
                return None
            await membership_svc.apply_self_activation_locked(
                expected_epoch=base + 1
            )
        await self.store.put_receipt(
            _receipt_token(envelope.nonce), {"applied": True, "epoch": base + 2}
        )
        return await self._finalize_target_activation_delivery(
            session, envelope, event, base=base
        )

    async def _find_target_session(
        self,
        space_id: str,
        source_fingerprint: str,
        *,
        pair_id: str = "",
        states: tuple[str, ...] = (
            MeshPairingState.TRANSFERRING.value,
            MeshPairingState.AWAITING_ACKS.value,
            MeshPairingState.BLOCKED_RECOVERY.value,
            MeshPairingState.ACTIVE.value,
        ),
    ) -> Optional[MeshPairingSession]:
        if pair_id:
            if type(pair_id) is not str or _PAIR_ID_RE.fullmatch(pair_id) is None:
                return None
            candidate = await self.store.get_session(pair_id)
            candidates = [] if candidate is None else [candidate]
        else:
            # Before target finalization the durable reservation is the exact
            # local space->pair authority and survives imported membership's
            # intentional incarnation stripping. This keeps legacy activation
            # events targeted even with arbitrarily large terminal history.
            reserved_pair_id = await self.store.get_reservation(space_id)
            if reserved_pair_id is not None:
                candidate = await self.store.get_session(reserved_pair_id)
                candidates = [] if candidate is None else [candidate]
            else:
                # Post-release compatibility only. Healthy reconfirmation can
                # validate an old event from membership alone if this bounded
                # legacy history is no longer enumerable.
                try:
                    candidates = await self.store.list_sessions(
                        max_sessions=MAX_PAIRING_SESSIONS
                    )
                except MeshPairingStoreError as exc:
                    if exc.code == "too_many_sessions":
                        return None
                    raise
        for session in candidates:
            if (
                session.role == MeshPairingRole.TARGET.value
                and session.space_id == space_id
                and session.source_fingerprint == source_fingerprint
                and session.state in states
            ):
                return session
        return None

    async def _finalize_target_activation(
        self, session: MeshPairingSession, *, base: int
    ) -> HandlerResult:
        """Idempotently complete THIS target's e+2 activation and return the signed
        ``{state: active, epoch: e+2}`` confirmation the source verifies.

        The local finalize tail signs the terminal e+2 receipt, flips node
        HEALTHY, and transitions the session ``-> active``.  The target
        reservation deliberately remains in place until a separate
        source-signed all-ACK confirmation arrives: releasing it here would let a
        target-side BANK_COMMIT race a source crash before its own terminal
        persistence.  Every local write is idempotent, so it is safe to re-run
        after a crash anywhere inside it. It is
        called from the fresh self-activation, the ``try_pending_self_activation``
        idempotent branch (node still UNSAFE), AND the healthy-path
        ``try_activation_reconfirmation`` (node already HEALTHY). Keying every
        convergence path on the ``MembershipView`` authority (membership already at
        e+2 with self ACTIVE) — not on session bookkeeping — is what guarantees a
        crash mid-tail still converges BOTH sides on resume.
        """

        marker_matches = await self._import_validation_matches(session, base=base)
        receipt_matches = (
            False
            if marker_matches
            else await self._target_activation_receipt_matches(session, base=base)
        )
        if not marker_matches and not receipt_matches:
            await self._mark_target_import_validation_failure(session)
            return _refuse(MeshResponseCode.LOCAL_UNSAFE)
        # Pair locks are deliberately not taken here: run_target_enrollment and
        # resync hold them while an inbound activation is in flight.  The
        # space-scoped tail lock instead linearizes this terminal receipt with
        # _import_and_await's TRANSFERRING -> AWAITING_ACKS write.
        async with self.store.space_lock(session.space_id):
            fresh = await self.store.get_session(session.pair_id)
            if fresh is None or fresh.role != MeshPairingRole.TARGET.value:
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            if fresh.state == MeshPairingState.TRANSFERRING.value:
                # The signed e+2 has already passed the import authority gate;
                # make the normally separate workflow edge explicit before the
                # terminal transition so the strict session graph remains true.
                fresh = fresh.transition(
                    MeshPairingState.AWAITING_ACKS, now_ms=self._clock_ms()
                )
                await self.store.put_session(fresh)
            if fresh.state not in (
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
                MeshPairingState.ACTIVE.value,
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            # The preflight above intentionally runs before taking the space
            # lock, so it cannot hold the finalization critical section across
            # storage I/O.  Re-check the durable import authority against the
            # freshly read session here: a concurrent resync may have cleared
            # the marker and moved this target back to TRANSFERRING while a
            # stale e+2 delivery was waiting for this lock.
            marker_matches = await self._import_validation_matches(fresh, base=base)
            receipt_matches = (
                False
                if marker_matches
                else await self._target_activation_receipt_matches(fresh, base=base)
            )
            if not marker_matches and not receipt_matches:
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            store = self._hive_store(session.space_id)
            # Persist a target-signed completion proof before any terminal
            # workflow/health tail.  If the import marker is later damaged, only
            # this immutable receipt — never a mutable ACTIVE session — may
            # re-authorize an idempotent e+2 confirmation.
            if marker_matches:
                await self._persist_target_activation_receipt(fresh, base=base)
            # HEALTHY precedes the terminal session receipt.  If this write
            # fails, the target remains awaiting/blocked and the source can
            # safely retry; writing ACTIVE first would orphan an UNSAFE e+2
            # target behind its still-held reservation.
            await store.set_node_status(
                NodeHealth(status=HiveNodeStatus.HEALTHY, reason="mesh_active")
            )
            if fresh.state in (
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
            ):
                active = fresh.transition(MeshPairingState.ACTIVE, now_ms=self._clock_ms())
                await self.store.put_session(active)
            # Source terminal confirmation releases the exact space->pair
            # reservation.  Until then this target may be e+2/HEALTHY but still
            # cannot perform ordinary writes, preserving the original signed
            # import boundary through full-mesh all-ACK convergence.
            return _ok(
                await self._target_activation_response_payload(fresh, base=base)
            )

    async def _prove_and_complete_active_target_tail(
        self, session: MeshPairingSession, *, base: int
    ) -> HandlerResult:
        """Complete an ACTIVE target tail only with durable local authority.

        Unlike :meth:`_complete_active_target_tail`, this path is entered from
        a local admin retry rather than an already-verified signed activation
        event.  It must therefore prove the retained import authority and the
        exact target e+2 membership while holding the same space-tail lock as
        terminal activation before it can restore HEALTHY.  It intentionally
        cannot release the reservation: only a source-signed terminal receipt
        can prove the remote all-ACK tail has persisted.
        """

        async with self.store.space_lock(session.space_id):
            fresh = await self.store.get_session(session.pair_id)
            if (
                fresh is None
                or fresh.role != MeshPairingRole.TARGET.value
                or fresh.state != MeshPairingState.ACTIVE.value
                or fresh.space_id != session.space_id
                or fresh.base_epoch != base
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            if (
                not await self._import_validation_matches(fresh, base=base)
                and not await self._target_activation_receipt_matches(
                    fresh, base=base
                )
                and not await self._target_finalized_activation_matches(
                    fresh, base=base
                )
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            store = self._hive_store(fresh.space_id)
            node, membership = await asyncio.gather(
                store.get_node_identity(), store.get_membership()
            )
            if (
                node is None
                or membership is None
                or membership.epoch != base + 2
                or node.node_id != _node_id_from_fingerprint(fresh.target_fingerprint)
                or not any(
                    member.node_id == node.node_id
                    and member.status == MemberStatus.ACTIVE.value
                    for member in membership.members
                )
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            await store.set_node_status(
                NodeHealth(status=HiveNodeStatus.HEALTHY, reason="mesh_active")
            )
            return _ok(await self._target_activation_response_payload(fresh, base=base))

    async def _finalize_target_activation_delivery(
        self,
        session: MeshPairingSession,
        envelope: MeshRequestEnvelope,
        event: EventEnvelope,
        *,
        base: int,
    ) -> HandlerResult:
        """Finalize local e+2, then consume an optional source terminal receipt."""

        finalized = await self._finalize_target_activation(session, base=base)
        if finalized.code is not MeshResponseCode.OK:
            return finalized
        confirmed = await self._complete_target_source_terminal_confirmation(
            session, envelope, event, base=base
        )
        return finalized if confirmed is None else confirmed

    async def try_activation_reconfirmation(
        self, envelope: MeshRequestEnvelope, event
    ) -> Optional[HandlerResult]:
        """Idempotent e+2 re-confirmation on the AUTHORIZED (node-HEALTHY) event
        path, called at the top of :meth:`handle_event`.

        When the target already applied its e+2 activation and flipped HEALTHY, a
        source that crashed before persisting its own ACTIVE session re-delivers the
        activation via ``resume``. That re-delivery is now authorized (healthy,
        active member) and reaches the GENERIC event pipeline, so without this hook
        it would get an ordinary ``{applied: true}`` ack that ``_deliver_activation``
        rejects — stranding the source forever. This returns the SAME signed
        ``{state: active, epoch: e+2}`` confirmation (finalizing the tail if a prior
        crash left it partial), and ``None`` for every other event (which then falls
        through to the generic append unchanged).
        """

        # Cheap short-circuit before any session scan: only a MEMBERSHIP_UPDATED
        # event promoting THIS node to ACTIVE can be our own activation.
        if getattr(event, "type", None) != EventType.MEMBERSHIP_UPDATED.value:
            return None
        store = self._hive_store(envelope.space_id)
        node = await store.get_node_identity()
        membership = await store.get_membership()
        if node is None or membership is None:
            return None
        self_id = node.node_id
        payload = event.payload if isinstance(event.payload, dict) else {}
        if (
            payload.get("node_id") != self_id
            or payload.get("status") != MemberStatus.ACTIVE.value
        ):
            return None
        # Membership authority: self must ALREADY be ACTIVE at e+2 (idempotent
        # re-confirmation only; this path never mutates membership).
        self_member = next((m for m in membership.members if m.node_id == self_id), None)
        if self_member is None or self_member.status != MemberStatus.ACTIVE.value:
            return None
        if membership.epoch < 2 or event.membership_epoch != membership.epoch:
            return None
        if payload.get("epoch") != membership.epoch:
            return None
        # Only now (rare: an event promoting us) resolve the key-bound session.
        session = await self._find_target_session(
            envelope.space_id,
            envelope.source_fingerprint,
            pair_id=payload.get("pair_id", ""),
            states=(
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
                MeshPairingState.ACTIVE.value,
            ),
        )
        # Same eligibility floor as try_pending_self_activation: the event origin
        # must be an ACTIVE COMMIT member (not any active member), and the
        # source-signed candidate-view digest must match our own recomputed view.
        if not _source_event_is_eligible(
            membership, envelope.source_public_key, event.origin_node_id
        ):
            return None
        if payload.get("candidate_view_digest") != candidate_view_digest(membership):
            return None
        if session is None:
            # A fully finalized pre-pair_id activation has no reservation left,
            # and an upgraded target may have more terminal history than the
            # bounded compatibility slice. Membership + HEALTHY routing already
            # prove self ACTIVE at this exact signed epoch; no session field is
            # needed by the source confirmation contract.
            if payload.get("pair_id", ""):
                return None
            await self.store.put_receipt(
                _receipt_token(envelope.nonce),
                {"applied": True, "epoch": membership.epoch},
            )
            return _ok(
                {"state": MeshPairingState.ACTIVE.value, "epoch": membership.epoch}
            )
        if membership.epoch != session.base_epoch + 2:
            return None
        # A completed target may have retained the source receipt and its own
        # terminal confirmation while losing only the detached target receipt.
        # Repair that exact local copy *before* interpreting a bare e+2 retry:
        # otherwise the source's first reconfirmation request would fence this
        # target, and a later terminal replay could no longer reconcile a
        # timestamp-variant source receipt after normal BANK_COMMIT progress.
        # The helper requires both existing terminal signatures and only writes
        # the embedded original target receipt; it cannot authorize a pending
        # first activation.
        await self._restore_target_activation_receipt_from_source_receipt(
            session,
            base=session.base_epoch,
            require_terminal_confirmation=True,
        )
        if "source_activation_receipt" in payload:
            signed_source = await self._source_activation_receipt_for_event(
                session, envelope, event, base=session.base_epoch
            )
            if signed_source is None or not await self._restore_target_activation_receipt_from_source_receipt(
                session,
                base=session.base_epoch,
                signed_source=signed_source,
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
            # A fully completed all-ACK tail may legitimately have moved its
            # BANK_COMMIT head.  If a crash/corruption removed one of the local
            # terminal receipt copies, restore the exact source-signed chain
            # before the strict e+1 gate below.  The helper is confined to an
            # already ACTIVE target with no reservation; it cannot authorize a
            # first promotion or a pending final-ACK tail.
            await self._restore_target_final_confirmation_from_source_receipt(
                session,
                signed_source,
                base=session.base_epoch,
            )
        if (
            not await self._import_validation_matches(session, base=session.base_epoch)
            and not await self._target_activation_receipt_matches(
                session, base=session.base_epoch
            )
            and not await self._target_finalized_activation_matches(
                session, base=session.base_epoch
            )
        ):
            # The router already authenticated this e+2 source event while the
            # target was HEALTHY.  Do not accept the mutable workflow record as
            # a substitute; fence first, then reconstruct a terminal receipt
            # only from this event plus the retained signed e+1 snapshot.
            await self._fence_target_activation_authority_loss(
                session, base=session.base_epoch
            )
            if not await self._restore_target_terminal_receipt_from_event(
                session, envelope, event, base=session.base_epoch
            ):
                return _refuse(MeshResponseCode.LOCAL_UNSAFE)
        if session.state == MeshPairingState.ACTIVE.value:
            # The signed event proves who sent this delivery, but a terminal
            # session remains mutable operational state.  Retained import
            # authority must still bind the original e+1 snapshot before an
            # inbound reconfirmation may restore HEALTHY or release the fence.
            completed = await self._prove_and_complete_active_target_tail(
                session, base=session.base_epoch
            )
            if completed.code is not MeshResponseCode.OK:
                return completed
            confirmed = await self._complete_target_source_terminal_confirmation(
                session, envelope, event, base=session.base_epoch
            )
            if confirmed is not None:
                completed = confirmed
            await self.store.put_receipt(
                _receipt_token(envelope.nonce),
                {"applied": True, "epoch": session.base_epoch + 2},
            )
            return completed
        await self.store.put_receipt(
            _receipt_token(envelope.nonce), {"applied": True, "epoch": session.base_epoch + 2}
        )
        return await self._finalize_target_activation_delivery(
            session, envelope, event, base=session.base_epoch
        )


class _LocalKeypair:
    """Minimal ``local_keypair`` carrying only the public key for import."""

    __slots__ = ("public_key",)

    def __init__(self, public_key: str) -> None:
        self.public_key = public_key


def _unb64(value: str) -> bytes:
    if type(value) is not str:
        raise MeshPairingServiceError("invalid_b64", "expected base64url string")
    try:
        return base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise MeshPairingServiceError("invalid_b64", "invalid base64url") from exc


def _legacy_membership_key(mesh_public_key: str) -> str:
    """Convert a Mesh ``ed25519-public:v1:`` key to the legacy ``ed25519:``
    membership encoding that ``_load_public_key`` / ``peer._verify`` accept.

    Both encode the SAME raw 32 bytes; membership uses the legacy form while the
    signed artifacts use the Mesh v1 form. ``decode_membership_public_key``
    matches either against the mesh identity by raw bytes.
    """

    raw = decode_mesh_public_key(mesh_public_key)
    return "ed25519:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _node_id_from_fingerprint(fingerprint: str) -> str:
    # hm1:<64hex> -> the 64-hex node id (stable, unique per target identity).
    return fingerprint.split(":", 1)[1]


def _membership_event_id(space_id: str, node_id: str, epoch: int) -> str:
    return uuid.uuid5(
        uuid.NAMESPACE_OID, f"{space_id}:membership_updated:{node_id}:{epoch}"
    ).hex


def _receipt_token(nonce: str) -> str:
    return "act_" + hashlib.sha256(nonce.encode("ascii")).hexdigest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _source_eligible(membership, source_public_key: str) -> bool:
    return _source_event_is_eligible(membership, source_public_key, None)


def _source_event_is_eligible(
    membership, source_public_key: str, origin_node_id: str | None
) -> bool:
    """Check the source key and, for unsafe-tail recovery, exact event origin."""

    try:
        source_raw = decode_membership_public_key(source_public_key)
        matches = [
            m
            for m in membership.members
            if decode_membership_public_key(m.public_key) == source_raw
        ]
    except Exception:
        return False
    if len(matches) != 1:
        return False
    member = matches[0]
    return (
        member.status == MemberStatus.ACTIVE.value
        and member.has_scope(PeerScope.COMMIT)
        and (origin_node_id is None or member.node_id == origin_node_id)
    )


__all__ = ["MeshPairingService", "MeshPairingServiceError", "HandlerResult"]
