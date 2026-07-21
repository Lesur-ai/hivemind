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

import base64
import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..core.hivemind import (
    BootstrapError,
    BootstrapService,
    EventEnvelope,
    EventType,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MemberStatus,
    MembershipEpochError,
    MembershipIncarnationError,
    MembershipService,
    NodeHealth,
    active_members,
)
from ..core.hivemind.models import PeerScope
from ..core.reservation_guard import PairingActivationError
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
    SignedMeshBootstrapEnvelope,
    build_bootstrap,
    import_bootstrap,
)
from .canonical import canonical_dumps, canonical_loads
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
    MeshPairingRole,
    MeshPairingSession,
    MeshPairingState,
    SignedBlockedRecoveryEvidence,
)
from .pairing_store import MeshPairingStore
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

    # ==================================================================
    # Pairing-activation fence (registered into the membership layer)
    # ==================================================================

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

        It lists sessions durably (bounded by the node's peer/pairing count) on each
        call — operator membership mutations are rare admin actions, not a hot path.
        If the pairing store is unsafe/blipping the list RAISES, which fail-closes
        the mutation: unlike the write-reservation guard (space-scoped via a per-
        space key), there is no space->session index, so a store outage refuses
        operator epoch-advancing mutations process-wide until it recovers. That is
        the safe default — a split cannot be risked while pairing state is
        unverifiable — and V1 is mono-process, so the blast radius is a rare admin
        op, never ordinary bank writes (those use the per-space reservation guard)."""

        for session in await self.store.list_sessions():
            if (
                session.role == MeshPairingRole.SOURCE.value
                and session.space_id == space_id
                and session.pair_id != ignore_pair_id
                and session.state
                in (
                    MeshPairingState.AWAITING_ACKS.value,
                    MeshPairingState.BLOCKED_RECOVERY.value,
                )
            ):
                raise PairingActivationError(space_id)

    # ==================================================================
    # SOURCE — Action 1: create invitation
    # ==================================================================

    async def create_invitation(self, space_id: str, *, requested_scopes: tuple[str, ...]) -> dict:
        """Issue a signed one-time invitation for ``space_id`` (source side)."""

        store = self._hive_store(space_id)
        node = await store.get_node_identity()
        membership = await store.get_membership()
        if node is None or membership is None or not active_members(membership):
            raise MeshPairingServiceError("not_meshable", "space is not an eligible Mesh source")
        # Project Mesh V1 pairs a two-node mesh from a single-node source: the
        # source must be the SOLE active member, so its own local application of
        # each fenced epoch bump IS the full-mesh all-ACK. A >1-active-member
        # source would advance shared membership the other members never acked;
        # that (multi-party all-ACK) is out of scope for V1 and refused closed.
        if len(active_members(membership)) != 1:
            raise MeshPairingServiceError(
                "multi_member_source", "V1 Mesh pairing requires a single active-member source"
            )
        health = await store.get_node_status()
        if health is not None and HiveNodeStatus(health.status) != HiveNodeStatus.HEALTHY:
            raise MeshPairingServiceError("unhealthy", "source space is not healthy")
        # The source must be an active member keyed with this instance identity.
        configured_raw = decode_membership_public_key(self._config.public_key)
        if not any(
            m.status == MemberStatus.ACTIVE.value
            and decode_membership_public_key(m.public_key) == configured_raw
            for m in membership.members
        ):
            raise MeshPairingServiceError("identity_mismatch", "instance identity is not an active member")

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
    ) -> dict:
        """Verify an invitation, reserve the blank target, and send a join claim.

        The raw one-time ``secret`` (out-of-band, transport-only) proves possession
        to the source; ``source_endpoint`` is where the source peer is reached.
        """

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

        pair_id = invitation.pair_id
        # Reserve the blank target under its space lock, proving virginity first
        # (V1 never merges a populated space into a cluster).
        async with self.store.space_lock(target_space_id):
            try:
                await self._bootstrap()._assert_blank_target(target_space_id)
            except BootstrapError as exc:
                raise MeshPairingServiceError("populated_target", "target space is not blank") from exc
            await self.store.reserve(target_space_id, pair_id, now_ms=now)

        # A raw invitation secret must accompany the invitation bytes out of band;
        # the caller passes it separately via ``invitation_bytes`` framing is not
        # used here — the target console supplies the secret to prove possession.
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
            requested_scopes=tuple(sorted(set(requested_scopes) | {"read"})),
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
            source_endpoint=source_endpoint,
            target_public_key=self._config.public_key,
            target_fingerprint=self._config.fingerprint,
            target_endpoint=self._config.public_url,
            granted_scopes=tuple(sorted(set(requested_scopes) | {"read"})),
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
        await self.store.put_blob(pair_id, "invitation", invitation_bytes)
        await self.store.put_blob(pair_id, "claim", signed_claim.canonical_bytes())
        await self.store.put_session(session)

        # Send the signed claim + the transport-only secret to the source peer.
        claim_body = canonical_dumps(
            {
                "claim": canonical_loads(signed_claim.canonical_bytes()),
                "secret": secret,
                "target_endpoint": self._config.public_url,
            }
        )
        response = await self._client(source_endpoint).claim(
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
        session = await self.store.get_session(pair_id)
        if session is None:
            raise MeshPairingServiceError("unknown_pair", "unknown pairing")
        await self.store.put_session(
            session.with_fields(now_ms=self._clock_ms(), source_endpoint=source_endpoint)
        )

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
                return {"pair_id": pair_id, "state": state, "ack_status": 200}
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
            if session.state == MeshPairingState.TRANSFERRING.value:
                session = await self._block_recovery(
                    session,
                    phase="bootstrap_import_failed",
                    next_action="resync",
                    manifest_digest=session.bootstrap_manifest_digest,
                )
            if session.state != MeshPairingState.BLOCKED_RECOVERY.value:
                raise MeshPairingServiceError("not_resyncable", "pairing is not resyncable")
            signed_ev = await self._verified_blocked_evidence(session)
            if (
                signed_ev.evidence.next_action != "resync"
                or signed_ev.evidence.phase != "bootstrap_import_failed"
            ):
                # A resume-class block (e.g. activation_unconfirmed) is the SOURCE's
                # to recover; a target never tears its space down for those.
                raise MeshPairingServiceError("not_resyncable", "blocked recovery is not a resync")
            # Mark UNSAFE BEFORE any replacement write, tear the space down to blank
            # (UNSAFE marker deleted LAST), then re-fetch + re-import from the source.
            store = self._hive_store(session.space_id)
            await store.set_node_status(
                NodeHealth(status=HiveNodeStatus.UNSAFE, reason="mesh_resync")
            )
            try:
                await self._teardown_target_space(session.space_id)
                signed_env = await self._fetch_and_verify_approval(session)
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
            awaiting = await self._import_and_await(transferring, signed_env)
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
                if await self.store.get_reservation(session.space_id) == pair_id:
                    await self._teardown_target_space(session.space_id)
                    await self.store.release(session.space_id, pair_id)
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
        self, session: MeshPairingSession
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
        signed_approval = SignedMeshArtifact.from_bytes(_unb64(status["approval_b64"]))
        signed_env = SignedMeshBootstrapEnvelope.from_bytes(_unb64(status["bootstrap_envelope_b64"]))
        signed_env.verify()
        # A valid source signature is NOT sufficient: bind the envelope to THIS
        # pairing so a validly-signed bootstrap for another space or addressed to a
        # different target cannot be imported into this reserved target space. The
        # target fingerprint is our own; the epoch is the post-admission e+1.
        env = signed_env.envelope
        if (
            env.space_id != session.space_id
            or env.source_public_key != session.source_public_key
            or env.source_fingerprint != session.source_fingerprint
            or env.target_fingerprint != session.target_fingerprint
            or env.target_fingerprint != self._config.fingerprint
            or env.membership_epoch != session.base_epoch + 1
        ):
            raise MeshPairingServiceError("bad_binding", "bootstrap envelope is not bound to this pairing")
        invitation_bytes = await self.store.get_blob(session.pair_id, "invitation")
        claim_bytes = await self.store.get_blob(session.pair_id, "claim")
        if invitation_bytes is None or claim_bytes is None:
            raise MeshPairingServiceError("missing_artifacts", "target artifacts are missing")
        verify_artifact_chain(
            SignedMeshArtifact.from_bytes(invitation_bytes),
            SignedMeshArtifact.from_bytes(claim_bytes),
            signed_approval,
        )
        return signed_env

    async def _import_and_await(
        self, transferring: MeshPairingSession, signed_env: SignedMeshBootstrapEnvelope
    ) -> MeshPairingSession:
        """Fetch + import the bootstrap into a TRANSFERRING target, then advance to
        AWAITING_ACKS. Any post-admission import failure is fail-closed into
        ``blocked_recovery`` with signed ``resync`` evidence — never a raw error
        that could strand the source's pending member."""

        client = self._client(transferring.source_endpoint)
        base = transferring.base_epoch
        try:
            boot_resp = await client.fetch_bootstrap(
                space_id=transferring.space_id,
                epoch=base,
                target_fingerprint=transferring.source_fingerprint,
                pair_id=transferring.pair_id,
            )
            if boot_resp.status_code != 200:
                raise MeshPairingServiceError("no_bootstrap", "source has no bootstrap")
            boot_payload = self._verify_source_response(
                boot_resp, source_fingerprint=transferring.source_fingerprint, correlation_id=transferring.pair_id
            )
            await import_bootstrap(
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
        except Exception as exc:
            await self._block_recovery(
                transferring,
                phase="bootstrap_import_failed",
                next_action="resync",
                manifest_digest=signed_env.envelope.manifest_digest,
            )
            raise MeshPairingServiceError(
                "import_failed", "bootstrap import failed; pairing is in blocked recovery"
            ) from exc
        awaiting = transferring.transition(MeshPairingState.AWAITING_ACKS, now_ms=self._clock_ms())
        await self.store.put_session(awaiting)
        return awaiting

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

        storage = self._storage_factory()
        prefix = f"{space_id}/"
        placeholders = {"", "_meta.json", "_rules.md", "live/.keep", "bank/.keep"}
        status_rel = "_hivemind/node_status.json"
        objects = await storage.list_objects(prefix)
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
        if session.state != MeshPairingState.ISSUED.value:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        now = self._clock_ms()
        if now >= session.expires_at_ms:
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
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
            or claim.invitation_digest != session.invitation_digest
            or envelope.source_public_key != claim.target_public_key
        ):
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        if claim.membership_epoch != session.base_epoch:
            return _refuse(MeshResponseCode.EPOCH_MISMATCH)
        secret = decoded["secret"]
        if type(secret) is not str or not verify_invitation_secret(
            secret, session.secret_digest, pair_id=pair_id, space_id=session.space_id
        ):
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        endpoint = decoded["target_endpoint"]
        if type(endpoint) is not str or not endpoint:
            return _refuse(MeshResponseCode.INVALID_EVENT)
        # Serialize the burn + session mutation so two concurrent claims bearing
        # the same valid secret cannot both proceed (one-time atomicity, no
        # last-writer-wins on the target binding). The per-pair lock is the
        # session tier of the session->reservation->membership order; this handler
        # makes no outbound call while holding it.
        async with self.store.pair_lock(pair_id):
            fresh = await self.store.get_session(pair_id)
            if fresh is None or fresh.state != MeshPairingState.ISSUED.value:
                return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
            if await self.store.is_secret_burned(pair_id):
                return _refuse(MeshResponseCode.REPLAY_REJECTED)
            if not await self.store.record_nonce(claim.nonce, now_ms=now):
                return _refuse(MeshResponseCode.REPLAY_REJECTED)
            await self.store.burn_secret(pair_id, fresh.secret_digest, now_ms=now)
            granted = tuple(
                sorted((set(claim.requested_scopes) & set(fresh.granted_scopes)) | {"read"})
            )
            await self.store.put_blob(pair_id, "claim", signed_claim.canonical_bytes())
            updated = fresh.transition(
                MeshPairingState.CLAIMED,
                now_ms=now,
                target_public_key=claim.target_public_key,
                target_fingerprint=claim.target_fingerprint,
                target_endpoint=endpoint,
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
        await self.store.put_blob(pair_id, "approval", signed_approval.canonical_bytes())
        # Approval signed but Transition 1 not yet committed (still pre-mutation:
        # a crash here can cancel/resume without touching shared membership).
        approved_session = session.transition(
            MeshPairingState.APPROVED, now_ms=now, approval_digest=signed_approval.digest()
        )
        await self.store.put_session(approved_session)

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
        transferring = approved_session.transition(MeshPairingState.TRANSFERRING, now_ms=now)
        # Hold the MEMBERSHIP lock across Transition 1 (admit) AND the bootstrap
        # export so a concurrent membership mutation (e.g. re-scope) cannot advance
        # the epoch between them and make the exported snapshot carry an epoch the
        # target rejects. The admit is a compare-and-admit at exactly base_epoch: a
        # concurrent mutation BEFORE admission fails closed (epoch_changed), and
        # holding the lock through export prevents one AFTER admission.
        async with membership_svc.space_lock():
            try:
                # Transition 1: admit the target PENDING (e -> e+1). The durable
                # per-incarnation tag (pair_id) lets a retained pairing force-evict
                # ONLY the incarnation it activated.
                await membership_svc.admit_pending_candidate_locked(
                    Member(
                        node_id=target_node_id,
                        public_key=_legacy_membership_key(session.target_public_key),
                        scopes=list(session.granted_scopes),
                        incarnation=pair_id,
                    ),
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

        snapshot = await self._bootstrap().export_snapshot(session.space_id)
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
        # Only the enrolled target may read pairing status / approval / bootstrap
        # metadata. A valid mesh keypair + a leaked pair_id is not sufficient.
        if envelope.source_public_key != session.target_public_key:
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        payload: dict[str, Any] = {"pair_id": pair_id, "state": session.state}
        if session.state in (
            MeshPairingState.TRANSFERRING.value,
            MeshPairingState.AWAITING_ACKS.value,
            MeshPairingState.ACTIVE.value,
        ):
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
        if envelope.source_public_key != session.target_public_key:
            return _refuse(MeshResponseCode.SOURCE_NOT_AUTHORIZED)
        if session.state not in (
            MeshPairingState.TRANSFERRING.value,
            MeshPairingState.AWAITING_ACKS.value,
            MeshPairingState.ACTIVE.value,
        ):
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
        if envelope.source_public_key != session.target_public_key:
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
        # Idempotent: if already active, re-ack success.
        if session.state == MeshPairingState.ACTIVE.value:
            return _ok({"pair_id": pair_id, "state": session.state})
        if session.state not in (
            MeshPairingState.TRANSFERRING.value,
            MeshPairingState.AWAITING_ACKS.value,
        ):
            return _refuse(MeshResponseCode.OPERATION_UNAVAILABLE)
        now = self._clock_ms()
        target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
        e2 = session.base_epoch + 2
        store = self._hive_store(session.space_id)
        membership = await store.get_membership()
        node = await store.get_node_identity()
        term = await store.get_term()
        # Fail closed on ANY concurrent membership change: promote ONLY if the
        # source is still at exactly the admit epoch e+1 with the target PENDING.
        # A concurrent change leaves shared membership unchanged (no promotion),
        # per the P10-3 acceptance invariant.
        if membership is None or node is None:
            return _refuse(MeshResponseCode.LOCAL_UNSAFE)
        if membership.epoch != session.base_epoch + 1:
            return _refuse(MeshResponseCode.EPOCH_MISMATCH)
        target_member = next((m for m in membership.members if m.node_id == target_node_id), None)
        if target_member is None or target_member.status != MemberStatus.PENDING.value:
            return _refuse(MeshResponseCode.EPOCH_MISMATCH)
        # The e+2 candidate-view digest is computed from the PROJECTED promotion
        # of the current e+1 view (== the digest the source would sign post-
        # promote, and == the target's independent recompute).
        digest = candidate_view_digest(projected_promotion_view(membership, target_node_id))
        event = EventEnvelope(
            event_id=_membership_event_id(session.space_id, target_node_id, e2),
            request_id=self._request_id_factory(),
            type=EventType.MEMBERSHIP_UPDATED,
            origin_node_id=node.node_id,
            term=term.term if term is not None else 0,
            membership_epoch=e2,
            payload={
                "node_id": target_node_id,
                "epoch": e2,
                "status": MemberStatus.ACTIVE.value,
                "candidate_view_digest": digest,
            },
        )
        event_body = canonical_dumps(event.model_dump(mode="json"))
        # Persist the post-mutation intent (awaiting_acks + activation event id)
        # BEFORE the shared membership mutation, so a crash mid-Transition-2 is
        # recoverable via resume() and never a silent rollback.
        awaiting = session.transition(
            MeshPairingState.AWAITING_ACKS, now_ms=now, activation_event_id=event.event_id
        )
        await self.store.put_session(awaiting)
        # Transition 2: promote the target ACTIVE (e+1 -> e+2), idempotent. The
        # compare-and-promote at exactly base_epoch+1 (atomic under the membership
        # lock) fails closed if a concurrent membership mutation advanced the epoch
        # between the e+1 check above and this promotion — the activation event is
        # pinned at e+2, so promoting at any other epoch would strand the target.
        try:
            await self._membership(session.space_id).promote_pending_to_active(
                target_node_id,
                expected_epoch=session.base_epoch + 1,
                activation_pair_id=session.pair_id,
            )
        except (MembershipEpochError, PairingActivationError):
            # PairingActivationError is defense-in-depth (the single-in-flight gate
            # precludes a DIFFERENT mid-activation pairing); both fail closed here.
            return _refuse(MeshResponseCode.EPOCH_MISMATCH)
        client = self._client(session.target_endpoint)
        confirmed = await self._deliver_activation(session, event, event_body, e2, digest)
        if not confirmed:
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
        active = awaiting.transition(MeshPairingState.ACTIVE, now_ms=self._clock_ms())
        await self.store.put_session(active)
        return _ok({"pair_id": pair_id, "state": active.state, "epoch": e2})

    async def _deliver_activation(self, session, event, event_body: bytes, e2: int, digest: str) -> bool:
        """Deliver the e+2 activation and require a VERIFIED target confirmation.

        Returns True only when the target signed a response bound to this
        activation request and confirming it applied e+2 ACTIVE. An unverified or
        merely-2xx response from a misrouted/hostile endpoint is NOT proof.
        """

        try:
            response = await self._client(session.target_endpoint).deliver_event(
                space_id=session.space_id,
                epoch=e2,
                target_fingerprint=session.target_fingerprint,
                body=event_body,
                request_id=event.request_id,
            )
            if response.status_code not in (200, 202):
                return False
            resp_body = self._verify_source_response(
                response, source_fingerprint=session.target_fingerprint, correlation_id=event.request_id
            )
            confirm = canonical_loads(resp_body)
        except Exception:
            return False
        return (
            isinstance(confirm, dict)
            and confirm.get("state") == MeshPairingState.ACTIVE.value
            and confirm.get("epoch") == e2
        )

    async def resume(self, pair_id: str) -> dict:
        """Resume a source pairing stranded in ``blocked_recovery`` by idempotently
        re-delivering the e+2 activation to the target (no rollback, no re-admit).

        The membership is already at e+2 (Transition 2 committed before the failed
        delivery); this re-drives ONLY the delivery, which the target applies
        idempotently through the confined router branch.
        """

        async with self.store.pair_lock(pair_id):
            session = await self.store.get_session(pair_id)
            if session is None or session.role != MeshPairingRole.SOURCE.value:
                raise MeshPairingServiceError("unknown_pair", "unknown source pairing")
            # Recover a source stranded after Transition 1: either explicitly
            # blocked, or crashed mid-Transition-2 (awaiting_acks intent persisted).
            if session.state not in (
                MeshPairingState.BLOCKED_RECOVERY.value,
                MeshPairingState.AWAITING_ACKS.value,
            ):
                raise MeshPairingServiceError("not_blocked", "pairing is not recoverable")
            # A blocked pairing must carry verifiable signed evidence whose recorded
            # next_action is exactly 'resume' before we re-drive the e+2 activation.
            # An export-failed / operator-abandoned block (next_action='evict') has
            # a target that never bootstrapped, so resuming it would wrongly promote
            # an un-bootstrapped node ACTIVE — those are evict-only.
            if session.state == MeshPairingState.BLOCKED_RECOVERY.value:
                signed_ev = await self._verified_blocked_evidence(session)
                if signed_ev.evidence.next_action != "resume":
                    raise MeshPairingServiceError(
                        "not_resumable", "blocked recovery is not resumable; use evict"
                    )
            store = self._hive_store(session.space_id)
            membership = await store.get_membership()
            node = await store.get_node_identity()
            term = await store.get_term()
            target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
            e2 = session.base_epoch + 2
            # Fail closed on an unexpected epoch: only e+1 (re-promote) or e+2
            # (already promoted) are recoverable.
            if membership is None or membership.epoch not in (session.base_epoch + 1, e2):
                raise MeshPairingServiceError("unrecoverable_epoch", "pairing epoch is not recoverable")
            # Defense-in-depth against the promotion-fence bypass: if the epoch is
            # already at e+2 it MUST be because OUR promotion committed (target
            # ACTIVE). If the target is not ACTIVE at e+2, some other membership
            # mutation reached e+2 while our promotion was refused — re-driving the
            # fixed e+2 activation here would self-promote the target against a
            # different source roster and split the views. That is unrecoverable.
            if membership.epoch == e2:
                promoted = next(
                    (m for m in membership.members if m.node_id == target_node_id), None
                )
                if promoted is None or promoted.status != MemberStatus.ACTIVE.value:
                    raise MeshPairingServiceError(
                        "unrecoverable_epoch",
                        "epoch advanced to e+2 without this pairing's promotion",
                    )
            if membership.epoch < e2:
                # Compare-and-promote at exactly base_epoch+1 (atomic under the
                # membership lock): a concurrent mutation that advanced the epoch
                # between the check above and here fails closed rather than promoting
                # at the wrong epoch (which would strand the target at a fixed e+2).
                try:
                    await self._membership(session.space_id).promote_pending_to_active(
                        target_node_id,
                        expected_epoch=session.base_epoch + 1,
                        activation_pair_id=session.pair_id,
                    )
                except MembershipEpochError as exc:
                    raise MeshPairingServiceError(
                        "unrecoverable_epoch", "membership epoch changed during resume"
                    ) from exc
                except PairingActivationError as exc:
                    # Defense-in-depth (single-in-flight gate precludes a different
                    # mid-activation pairing); fail closed rather than 500.
                    raise MeshPairingServiceError(
                        "pairing_in_flight",
                        "another Mesh pairing for this space is mid-activation",
                    ) from exc
                membership = await store.get_membership()
            digest = candidate_view_digest(membership)
            event = EventEnvelope(
                event_id=session.activation_event_id or _membership_event_id(session.space_id, target_node_id, e2),
                request_id=self._request_id_factory(),
                type=EventType.MEMBERSHIP_UPDATED,
                origin_node_id=node.node_id,
                term=term.term if term is not None else 0,
                membership_epoch=e2,
                payload={
                    "node_id": target_node_id,
                    "epoch": e2,
                    "status": MemberStatus.ACTIVE.value,
                    "candidate_view_digest": digest,
                },
            )
            body = canonical_dumps(event.model_dump(mode="json"))
            confirmed = await self._deliver_activation(session, event, body, e2, digest)
            if not confirmed:
                # Could not converge (e.g. an awaiting_acks crash-window session, or
                # a re-failed delivery to a dead target). Persist blocked_recovery
                # with fresh signed evidence so the DURABLE state matches the
                # returned state and a genuinely dead ACTIVE target is reachable by
                # force_evict_member (whose gate requires blocked_recovery).
                if session.state != MeshPairingState.BLOCKED_RECOVERY.value:
                    await self._block_recovery(
                        session,
                        phase="activation_unconfirmed",
                        next_action="resume",
                        candidate_view_digest=digest,
                        activation_event_id=event.event_id,
                    )
                return {"pair_id": pair_id, "state": MeshPairingState.BLOCKED_RECOVERY.value}
            active = session.transition(MeshPairingState.ACTIVE, now_ms=self._clock_ms())
            await self.store.put_session(active)
            return {"pair_id": pair_id, "state": active.state, "epoch": e2}

    async def cancel(self, pair_id: str) -> dict:
        """Cancel a PRE-MUTATION pairing (issued/claimed/approved): releases the
        target reservation and leaves membership unchanged (PROJECT_MESH.md §7).
        A pairing past the shared-mutation boundary must use ``evict`` instead."""

        session = await self.store.get_session(pair_id)
        if session is None:
            raise MeshPairingServiceError("unknown_pair", "unknown pairing")
        if session.state_enum not in PRE_MUTATION_STATES:
            raise MeshPairingServiceError(
                "not_cancellable", "pairing passed the shared-mutation boundary; use evict"
            )
        # Reconcile with membership before treating an 'approved' source session as
        # pre-mutation: a crash between Transition 1 and the transferring persist can
        # leave 'approved' while the target is ALREADY admitted pending at e+1. That
        # is post-mutation — cancelling would release the reservation and strand the
        # pending member — so route it to evict instead.
        if session.role == MeshPairingRole.SOURCE.value and session.target_fingerprint:
            membership = await self._hive_store(session.space_id).get_membership()
            target_node_id = _node_id_from_fingerprint(session.target_fingerprint)
            if membership is not None and any(
                m.node_id == target_node_id
                and m.status in (MemberStatus.PENDING.value, MemberStatus.ACTIVE.value)
                for m in membership.members
            ):
                raise MeshPairingServiceError(
                    "already_admitted", "target already admitted; use evict"
                )
        async with self.store.pair_lock(pair_id):
            fresh = await self.store.get_session(pair_id)
            if fresh is None or fresh.state_enum not in PRE_MUTATION_STATES:
                raise MeshPairingServiceError("not_cancellable", "pairing is no longer cancellable")
            cancelled = fresh.transition(MeshPairingState.CANCELLED, now_ms=self._clock_ms())
            await self.store.put_session(cancelled)
            # A pre-mutation exit releases the target reservation (target role).
            if fresh.role == MeshPairingRole.TARGET.value:
                await self.store.release(fresh.space_id, pair_id)
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
            member = next((m for m in (membership.members if membership else []) if m.node_id == target_node_id), None)
            # A promoted (ACTIVE) target is convergence-only, never a pairing give-up:
            # its activation may already have applied on the target while the
            # confirmation was lost, so evicting here would split membership.
            if member is not None and member.status == MemberStatus.ACTIVE.value:
                raise MeshPairingServiceError(
                    "target_active",
                    "target is active in shared membership; resume to converge (do not evict a promoted member)",
                )
            admitted_pending = member is not None and member.status == MemberStatus.PENDING.value
            # Evictable: an explicitly blocked pairing, OR any non-terminal source
            # pairing whose target is admitted PENDING (a dangling candidate a cancel
            # would strand — 'approved' crash window, or a stuck 'transferring'/
            # 'awaiting_acks' whose target is gone). Never an active/terminal pairing.
            if session.state != MeshPairingState.BLOCKED_RECOVERY.value and not (
                admitted_pending
                and session.state
                in (
                    MeshPairingState.APPROVED.value,
                    MeshPairingState.TRANSFERRING.value,
                    MeshPairingState.AWAITING_ACKS.value,
                )
            ):
                raise MeshPairingServiceError("not_evictable", "only a blocked or dangling pairing may be evicted")
            # Route a post-mutation transfer/ack state through blocked_recovery first
            # (synthesizing signed evidence) so the abandonment is auditable; the
            # crash-window 'approved' and an already-blocked pairing transition to
            # cancelled directly.
            if session.state in (
                MeshPairingState.TRANSFERRING.value,
                MeshPairingState.AWAITING_ACKS.value,
            ):
                session = await self._block_recovery(
                    session, phase="operator_abandoned", next_action="evict"
                )
            if admitted_pending:
                try:
                    await self._membership(session.space_id).remove_pending_candidate(
                        target_node_id,
                        operator=operator,
                        reason=reason,
                        confirm=True,
                        activation_pair_id=session.pair_id,
                    )
                except PairingActivationError as exc:
                    # Defense-in-depth: this give-up bypasses its OWN session, and
                    # the single-in-flight gate precludes a different mid-activation
                    # pairing — but fail structured rather than 500 if one existed.
                    raise MeshPairingServiceError(
                        "pairing_in_flight",
                        "another Mesh pairing for this space is mid-activation",
                    ) from exc
            await self.store.release(session.space_id, session.pair_id)
            cancelled = session.transition(MeshPairingState.CANCELLED, now_ms=self._clock_ms())
            await self.store.put_session(cancelled)
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
            target_node_id = (
                _node_id_from_fingerprint(session.target_fingerprint) if session.target_fingerprint else ""
            )
            store = self._hive_store(session.space_id)
            membership = await store.get_membership()
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
        session = await self._find_target_session(envelope.space_id, envelope.source_fingerprint)
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
        node = await store.get_node_identity()
        membership = await store.get_membership()
        if node is None or membership is None:
            return None
        self_id = node.node_id
        payload = event.payload if isinstance(event.payload, dict) else {}
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
                await self.store.put_receipt(
                    _receipt_token(envelope.nonce), {"applied": True, "epoch": base + 2}
                )
                return await self._finalize_target_activation(session, base=base)
        # (d) exact e+1 -> e+2 and self is PENDING at e+1
        if membership.epoch != base + 1:
            return None
        self_member = next((m for m in membership.members if m.node_id == self_id), None)
        if self_member is None or self_member.status != MemberStatus.PENDING.value:
            return None
        # (c) the source (event origin) is an eligible ACTIVE commit member of the
        # LOCAL e+1 view
        if not _source_eligible(membership, envelope.source_public_key):
            return None
        # (e) the source-signed candidate-view digest matches the target's own
        # recomputed e+2 view
        try:
            projected = projected_promotion_view(membership, self_id)
        except ValueError:
            return None
        if payload.get("candidate_view_digest") != candidate_view_digest(projected):
            return None

        # All predicates hold: self-promote e+1 -> e+2, then run the idempotent
        # finalize tail (HEALTHY + reservation release + session -> active).
        await self._membership(session.space_id).apply_self_activation(expected_epoch=base + 1)
        await self.store.put_receipt(
            _receipt_token(envelope.nonce), {"applied": True, "epoch": base + 2}
        )
        return await self._finalize_target_activation(session, base=base)

    async def _find_target_session(
        self,
        space_id: str,
        source_fingerprint: str,
        *,
        states: tuple[str, ...] = (
            MeshPairingState.TRANSFERRING.value,
            MeshPairingState.AWAITING_ACKS.value,
            MeshPairingState.BLOCKED_RECOVERY.value,
        ),
    ) -> Optional[MeshPairingSession]:
        for session in await self.store.list_sessions():
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

        The finalize tail is three durable writes — flip node HEALTHY, release the
        target reservation, transition the session ``-> active`` — and every one is
        idempotent, so it is safe to re-run after a crash anywhere inside it. It is
        called from the fresh self-activation, the ``try_pending_self_activation``
        idempotent branch (node still UNSAFE), AND the healthy-path
        ``try_activation_reconfirmation`` (node already HEALTHY). Keying every
        convergence path on the ``MembershipView`` authority (membership already at
        e+2 with self ACTIVE) — not on session bookkeeping — is what guarantees a
        crash mid-tail still converges BOTH sides on resume.
        """

        store = self._hive_store(session.space_id)
        await store.set_node_status(
            NodeHealth(status=HiveNodeStatus.HEALTHY, reason="mesh_active")
        )
        # Release the target reservation so the newly active member accepts
        # ordinary writes (no-op if a prior partial finalize already released it).
        await self.store.release(session.space_id, session.pair_id)
        fresh = await self.store.get_session(session.pair_id)
        if fresh is not None and fresh.state != MeshPairingState.ACTIVE.value:
            # awaiting_acks -> active and blocked_recovery -> active are both legal;
            # a session in any other state (e.g. a transient transferring) is left
            # untouched — membership is authoritative and already converged.
            if fresh.state_enum in (
                MeshPairingState.AWAITING_ACKS,
                MeshPairingState.BLOCKED_RECOVERY,
            ):
                active = fresh.transition(MeshPairingState.ACTIVE, now_ms=self._clock_ms())
                await self.store.put_session(active)
        return _ok(
            {"pair_id": session.pair_id, "state": MeshPairingState.ACTIVE.value, "epoch": base + 2}
        )

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
            states=(
                MeshPairingState.AWAITING_ACKS.value,
                MeshPairingState.BLOCKED_RECOVERY.value,
                MeshPairingState.ACTIVE.value,
            ),
        )
        if session is None or membership.epoch != session.base_epoch + 2:
            return None
        # Same eligibility floor as try_pending_self_activation: the event origin
        # must be an ACTIVE COMMIT member (not any active member), and the
        # source-signed candidate-view digest must match our own recomputed view.
        if not _source_eligible(membership, envelope.source_public_key):
            return None
        if payload.get("candidate_view_digest") != candidate_view_digest(membership):
            return None
        await self.store.put_receipt(
            _receipt_token(envelope.nonce), {"applied": True, "epoch": session.base_epoch + 2}
        )
        return await self._finalize_target_activation(session, base=session.base_epoch)


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
    return member.status == MemberStatus.ACTIVE.value and member.has_scope(PeerScope.COMMIT)


__all__ = ["MeshPairingService", "MeshPairingServiceError", "HandlerResult"]
