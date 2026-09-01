# -*- coding: utf-8 -*-
"""Durable local-only Project Mesh pairing store (P10-3, issue #191).

Persists pairing sessions, target reservations, burned one-time-secret markers,
claim/approval nonce dedup, activation receipts (idempotency), and signed
blocked-recovery evidence under
``_system/mesh_pairing/<instance-fingerprint>/``.  Existing-space source
preparation evidence is the exception: it lives under the fingerprint-neutral
``_system/mesh_source_preparations/`` fence so an identity change cannot hide an
incomplete transition.

This is **local operational state** — not shared memory, not a long-memory
document, and not membership authority (PROJECT_MESH.md §4).  Every durable write
is verified by a byte-identical read-back; an ambiguous/non-identical persistence
result **poisons** the process-local store so all later operations fail closed
(mirrors the P10-2 ``DurableReplayLedger`` discipline).  An in-memory index
retains the target-reservation fast path; the fingerprint-neutral source-
preparation fence is deliberately read durably on every ordinary-write guard.
PREPARING is a temporary ordinary-write reservation; either PREPARING or
COMPLETE is permanent provenance that refuses any later DIRECT_LOCAL authority
and any reuse of the space id as a blank pairing target.
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Optional, Protocol

from ..core.reservation_guard import source_preparation_key
from .canonical import canonical_dumps, canonical_loads
from .pairing_state import (
    ImportValidatedAuthority,
    MeshPairingSession,
    SignedBlockedRecoveryEvidence,
    SignedSourceActivationMigrationAuthority,
    SignedSourceActivationReceipt,
    SignedSourceBootstrapEvidence,
    SignedSourcePendingEvictionIntent,
    SignedSourcePreClaimCancelBarrier,
    SignedSourceTerminalDispositionReceipt,
    SignedTargetPairingAdmissionAnchor,
    SignedTargetActivationReceipt,
    SignedTargetPairingFenceAuthority,
    SignedTargetTerminalConfirmationReceipt,
    SourcePreparationIntent,
    SourcePreparationState,
)

_PREFIX_RE = re.compile(r"^_system/mesh_pairing/hm1:[0-9a-f]{64}/$", re.ASCII)
_PAIR_ID_RE = re.compile(r"^pair_[0-9a-f]{32}$", re.ASCII)
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)
_KEY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$", re.ASCII)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
MAX_PAIRING_SESSIONS = 256
MAX_PAIRING_RESERVATIONS = 256
# Startup migration is deliberately bounded.  It runs once to materialize the
# O(1) target admission anchors for retained pre-index #417 intents; an overflow
# refuses startup rather than silently truncating authority provenance.
MAX_TARGET_ACCEPTANCE_INTENTS_MIGRATION = 4096
MAX_PAIRING_RECORD_BYTES = 65_536
_ACTIVATION_FENCE_PHASES = frozenset(
    {"activation", "source_terminal_confirmation"}
)

#: Process-local poison set (keyed by store prefix), permanent for the process.
_PAIRING_UNSAFE_PREFIXES: set[str] = set()


class MeshPairingStoreError(RuntimeError):
    """Machine-readable, non-reflective pairing-store refusal."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


class PairingStorage(Protocol):
    async def put(
        self, key: str, content: str, content_type: str = "text/plain; charset=utf-8"
    ) -> None: ...

    async def get(self, key: str) -> str | None: ...

    async def delete(self, key: str) -> None: ...

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]: ...


def _fail(code: str, message: str) -> "None":
    raise MeshPairingStoreError(code, message)


class MeshPairingStore:
    """Durable local pairing / reservation / secret / receipt / evidence store."""

    def __init__(self, storage: PairingStorage, *, prefix: str) -> None:
        if _PREFIX_RE.fullmatch(prefix) is None:
            raise ValueError("invalid Mesh pairing prefix")
        self._storage = storage
        self._prefix = prefix
        self._global_lock = asyncio.Lock()
        self._reserved_load_lock = asyncio.Lock()
        self._space_locks: dict[str, asyncio.Lock] = {}
        self._pair_locks: dict[str, asyncio.Lock] = {}
        # In-memory reservation index (space_id -> pair_id); ``None`` until first
        # load so the guard knows to hydrate once.
        self._reserved: Optional[dict[str, str]] = None

    # -- safety ------------------------------------------------------------

    @property
    def unsafe(self) -> bool:
        return self._prefix in _PAIRING_UNSAFE_PREFIXES

    def assert_safe(self) -> None:
        if self.unsafe:
            _fail("local_unsafe", "Mesh pairing store is unsafe")

    def _mark_unsafe(self) -> None:
        _PAIRING_UNSAFE_PREFIXES.add(self._prefix)

    def space_lock(self, space_id: str) -> asyncio.Lock:
        return self._space_locks.setdefault(space_id, asyncio.Lock())

    def pair_lock(self, pair_id: str) -> asyncio.Lock:
        return self._pair_locks.setdefault(pair_id, asyncio.Lock())

    # -- durable primitives ------------------------------------------------

    async def _durable_put(self, key: str, payload: dict) -> None:
        """Write ``payload`` (HCJ canonical) and verify a byte-identical readback.

        A mismatch or read failure poisons the store and fails closed — never a
        silent success.
        """

        self.assert_safe()
        raw = canonical_dumps(payload)
        text = raw.decode("utf-8")
        try:
            await self._storage.put(key, text)
            read = await self._storage.get(key)
        except Exception as exc:  # storage/backend error is ambiguous
            self._mark_unsafe()
            raise MeshPairingStoreError("io_ambiguous", "Mesh pairing write is unsafe") from exc
        if read is None or read.encode("utf-8") != raw:
            self._mark_unsafe()
            _fail("readback_mismatch", "Mesh pairing write failed read-back verification")

    async def _get_json(self, key: str) -> Optional[dict]:
        self.assert_safe()
        try:
            read = await self._storage.get(key)
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError("io_ambiguous", "Mesh pairing read is unsafe") from exc
        if read is None:
            return None
        value = canonical_loads(read.encode("utf-8"))
        if not isinstance(value, dict):
            _fail("corrupt_state", "Mesh pairing record is corrupt")
        return value

    # -- sessions ----------------------------------------------------------

    def _session_key(self, pair_id: str) -> str:
        if _PAIR_ID_RE.fullmatch(pair_id) is None:
            raise MeshPairingStoreError("invalid_pair_id", "invalid pair id")
        return f"{self._prefix}sessions/{pair_id}.json"

    async def put_session(self, session: MeshPairingSession) -> None:
        await self._durable_put(self._session_key(session.pair_id), session.as_dict())

    async def get_session(self, pair_id: str) -> Optional[MeshPairingSession]:
        data = await self._get_json(self._session_key(pair_id))
        if data is None:
            return None
        session = MeshPairingSession.from_dict(data)
        if session.pair_id != pair_id:
            _fail("corrupt_state", "Mesh pairing key does not match its record")
        return session

    async def list_sessions(
        self, *, max_sessions: int | None = MAX_PAIRING_SESSIONS
    ) -> list[MeshPairingSession]:
        self.assert_safe()
        if max_sessions is not None and (
            type(max_sessions) is not int or max_sessions <= 0
        ):
            _fail("invalid_limit", "invalid Mesh session limit")
        prefix = f"{self._prefix}sessions/"
        try:
            objects = await self._storage.list_objects(
                prefix,
                max_keys=(max_sessions + 1) if max_sessions is not None else 0,
            )
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError("io_ambiguous", "Mesh pairing list is unsafe") from exc
        if max_sessions is not None and len(objects) > max_sessions:
            _fail("too_many_sessions", "Mesh session inventory exceeds its bound")
        sessions: list[MeshPairingSession] = []
        for obj in objects:
            key = obj.get("Key") if isinstance(obj, dict) else None
            size = obj.get("Size") if isinstance(obj, dict) else None
            if (
                not isinstance(key, str)
                or not key.endswith(".json")
                or type(size) is not int
                or size < 0
                or size > MAX_PAIRING_RECORD_BYTES
            ):
                _fail(
                    "corrupt_state",
                    "Mesh pairing namespace contains an invalid entry",
                )
            data = await self._get_json(key)
            if data is not None:
                session = MeshPairingSession.from_dict(data)
                if self._session_key(session.pair_id) != key:
                    _fail("corrupt_state", "Mesh pairing key does not match its record")
                sessions.append(session)
        return sessions

    async def list_sessions_diagnostic(
        self, *, max_sessions: int = MAX_PAIRING_SESSIONS
    ) -> tuple[list[MeshPairingSession], bool]:
        """Return a bounded UI-only history slice plus an explicit truncation bit.

        Authority paths must use targeted records or exhaustive ``list_sessions``;
        a diagnostic slice is never suitable for a safety decision.
        """

        self.assert_safe()
        if type(max_sessions) is not int or max_sessions <= 0:
            _fail("invalid_limit", "invalid Mesh session limit")
        prefix = f"{self._prefix}sessions/"
        try:
            objects = await self._storage.list_objects(
                prefix, max_keys=max_sessions + 1
            )
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError(
                "io_ambiguous", "Mesh pairing diagnostic list is unsafe"
            ) from exc
        truncated = len(objects) > max_sessions
        sessions: list[MeshPairingSession] = []
        for obj in objects[:max_sessions]:
            key = obj.get("Key") if isinstance(obj, dict) else None
            size = obj.get("Size") if isinstance(obj, dict) else None
            if (
                not isinstance(key, str)
                or not key.endswith(".json")
                or type(size) is not int
                or size < 0
                or size > MAX_PAIRING_RECORD_BYTES
            ):
                _fail(
                    "corrupt_state",
                    "Mesh pairing diagnostic namespace contains an invalid entry",
                )
            data = await self._get_json(key)
            if data is not None:
                session = MeshPairingSession.from_dict(data)
                if self._session_key(session.pair_id) != key:
                    _fail("corrupt_state", "Mesh pairing key does not match its record")
                sessions.append(session)
        return sessions, truncated

    # -- existing-space source preparation -------------------------------

    def _source_preparation_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            raise MeshPairingStoreError("invalid_space_id", "invalid space id")
        # Deliberately independent of ``self._prefix`` / configured fingerprint:
        # a restart with changed identity must still observe and fence an intent
        # persisted before any Hivemind object existed.
        return source_preparation_key(space_id)

    async def get_source_preparation(
        self, space_id: str
    ) -> Optional[SourcePreparationIntent]:
        data = await self._get_json(self._source_preparation_key(space_id))
        if data is None:
            return None
        intent = SourcePreparationIntent.from_dict(data)
        if intent.space_id != space_id:
            _fail(
                "corrupt_state",
                "source preparation key does not match its record",
            )
        return intent

    @staticmethod
    def _same_source_preparation_evidence(
        left: SourcePreparationIntent, right: SourcePreparationIntent
    ) -> bool:
        left_data = left.as_dict()
        right_data = right.as_dict()
        for mutable in ("state", "completed_at_ms"):
            left_data.pop(mutable)
            right_data.pop(mutable)
        return left_data == right_data

    async def put_source_preparation(self, intent: SourcePreparationIntent) -> None:
        """Persist the sole legal source-preparation transition.

        Creation must start at PREPARING.  The only later write is the exact
        evidence-preserving PREPARING -> COMPLETE transition.  Exact retries do
        not rewrite bytes; a different identity/token/id is a closed conflict.
        A target reservation and source preparation are mutually exclusive.
        """

        if not isinstance(intent, SourcePreparationIntent):
            _fail("invalid_preparation", "invalid source preparation")
        async with self._global_lock:
            existing = await self.get_source_preparation(intent.space_id)
            if existing == intent:
                return

            if existing is None:
                if intent.state_enum is not SourcePreparationState.PREPARING:
                    _fail(
                        "illegal_transition",
                        "source preparation must begin in preparing state",
                    )
                reserved = await self._ensure_reserved_index()
                if intent.space_id in reserved:
                    _fail(
                        "space_reserved",
                        "space is reserved for a Project Mesh pairing",
                    )
            else:
                if not self._same_source_preparation_evidence(existing, intent):
                    _fail(
                        "preparation_conflict",
                        "source preparation evidence does not match",
                    )
                if existing.state_enum is SourcePreparationState.COMPLETE:
                    _fail(
                        "illegal_transition",
                        "completed source preparation is immutable",
                    )
                if intent != existing.complete(intent.completed_at_ms):
                    _fail(
                        "illegal_transition",
                        "invalid source preparation transition",
                    )

            await self._durable_put(
                self._source_preparation_key(intent.space_id), intent.as_dict()
            )

    # -- immutable bootstrap / import authorities -----------------------

    def _source_bootstrap_evidence_key(self, pair_id: str) -> str:
        return f"{self._prefix}source_bootstrap_evidence/{self._pair_token(pair_id)}.json"

    def _source_activation_marker_key(self, space_id: str) -> str:
        """Return the durable, per-space provenance key for a source tail.

        The marker intentionally carries the same signed export binding as the
        bootstrap-evidence record, but lives under a distinct key.  The latter
        is protocol authority for final source revalidation; this retained copy
        is an ordinary-write fence discriminator after a crash/corruption loses
        the mutable activation fence or a membership incarnation.
        """

        if _SPACE_ID_RE.fullmatch(space_id) is None:
            raise MeshPairingStoreError("invalid_space_id", "invalid space id")
        return f"{self._prefix}source_activation_markers/{space_id}.json"

    async def get_source_bootstrap_evidence(
        self, pair_id: str
    ) -> Optional[SignedSourceBootstrapEvidence]:
        data = await self._get_json(self._source_bootstrap_evidence_key(pair_id))
        if data is None:
            return None
        signed = SignedSourceBootstrapEvidence.from_dict(data)
        if signed.evidence.pair_id != pair_id:
            _fail("corrupt_state", "source bootstrap evidence key does not match its record")
        return signed

    async def put_source_bootstrap_evidence(
        self, signed: SignedSourceBootstrapEvidence
    ) -> None:
        """Persist a single exact source export binding.

        The record is immutable: a retry may prove it is byte-for-byte the same
        export, but no later source state may overwrite it to authorize a stale
        target ACK.
        """

        if not isinstance(signed, SignedSourceBootstrapEvidence):
            _fail("invalid_source_evidence", "invalid source bootstrap evidence")
        async with self._global_lock:
            evidence = signed.evidence
            existing = await self.get_source_bootstrap_evidence(evidence.pair_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.evidence.as_dict()
                evidence_data = evidence.as_dict()
                existing_data.pop("recorded_at_ms")
                evidence_data.pop("recorded_at_ms")
                if existing_data == evidence_data:
                    # A crash can strand APPROVED after the binding is durable
                    # but before its session/blob tail. Retrying the exact
                    # snapshot retains—not rewrites—the original proof.
                    return
                _fail(
                    "source_evidence_conflict",
                    "source bootstrap evidence is immutable",
                )
            await self._durable_put(
                self._source_bootstrap_evidence_key(evidence.pair_id),
                signed.as_dict(),
            )

    async def get_source_activation_marker(
        self, space_id: str
    ) -> Optional[SignedSourceBootstrapEvidence]:
        """Return a signed #417 source-tail provenance marker, if present."""

        data = await self._get_json(self._source_activation_marker_key(space_id))
        if data is None:
            return None
        signed = SignedSourceBootstrapEvidence.from_dict(data)
        if signed.evidence.space_id != space_id:
            _fail(
                "corrupt_state",
                "source activation marker key does not match its record",
            )
        return signed

    async def put_source_activation_marker(
        self, signed: SignedSourceBootstrapEvidence
    ) -> None:
        """Persist immutable source-tail provenance independently of the fence.

        This is not a second authority for promotion.  It is a signed, readback
        verified discriminator that lets the ordinary-write guard fail closed if
        an in-progress #417 source tail loses its mutable fence, membership
        incarnation, or primary evidence key.
        """

        if not isinstance(signed, SignedSourceBootstrapEvidence):
            _fail("invalid_source_evidence", "invalid source activation marker")
        async with self._global_lock:
            evidence = signed.evidence
            existing = await self.get_source_activation_marker(evidence.space_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.evidence.as_dict()
                evidence_data = evidence.as_dict()
                existing_data.pop("recorded_at_ms")
                evidence_data.pop("recorded_at_ms")
                if existing_data == evidence_data:
                    return
                _fail(
                    "source_activation_marker_conflict",
                    "source activation marker is immutable",
                )
            await self._durable_put(
                self._source_activation_marker_key(evidence.space_id),
                signed.as_dict(),
            )

    async def release_source_activation_marker(
        self, space_id: str, pair_id: str
    ) -> None:
        """Delete a completed source-tail provenance marker with readback."""

        async with self._global_lock:
            existing = await self.get_source_activation_marker(space_id)
            if existing is None:
                return
            if existing.evidence.pair_id != pair_id:
                _fail(
                    "source_activation_marker_conflict",
                    "another source pairing owns the activation marker",
                )
            key = self._source_activation_marker_key(space_id)
            self.assert_safe()
            try:
                await self._storage.delete(key)
                observed = await self._storage.get(key)
            except Exception as exc:
                self._mark_unsafe()
                raise MeshPairingStoreError(
                    "io_ambiguous", "source activation marker release is unsafe"
                ) from exc
            if observed is not None:
                self._mark_unsafe()
                _fail(
                    "delete_unconfirmed",
                    "source activation marker release was not confirmed",
                )

    def _import_validation_key(self, pair_id: str) -> str:
        return f"{self._prefix}import_validations/{self._pair_token(pair_id)}.json"

    async def get_import_validation(
        self, pair_id: str
    ) -> Optional[ImportValidatedAuthority]:
        data = await self._get_json(self._import_validation_key(pair_id))
        if data is None:
            return None
        authority = ImportValidatedAuthority.from_dict(data)
        if authority.pair_id != pair_id:
            _fail("corrupt_state", "import validation key does not match its record")
        return authority

    async def put_import_validation(
        self, authority: ImportValidatedAuthority
    ) -> None:
        """Persist a read-back-verified, immutable target import proof."""

        if not isinstance(authority, ImportValidatedAuthority):
            _fail("invalid_import_validation", "invalid import validation")
        async with self._global_lock:
            existing = await self.get_import_validation(authority.pair_id)
            if existing == authority:
                return
            if existing is not None:
                existing_data = existing.as_dict()
                authority_data = authority.as_dict()
                existing_data.pop("validated_at_ms")
                authority_data.pop("validated_at_ms")
                if existing_data == authority_data:
                    # A target resync can re-import the exact same immutable
                    # snapshot.  The original evidence remains the authority;
                    # do not rewrite it merely to refresh an observation time.
                    return
                _fail(
                    "import_validation_conflict",
                    "import validation is immutable",
                )
            await self._durable_put(
                self._import_validation_key(authority.pair_id),
                authority.as_dict(),
            )

    async def clear_import_validation_for_resync(self, pair_id: str) -> None:
        """Remove a rejected target marker immediately before a fresh signed import.

        This is intentionally narrower than a general mutable-record API.  The
        pairing service invokes it only after it has put the target UNSAFE and
        verified signed ``import_validation_failed``/``resync`` evidence; the
        following import writes a newly read-back-verified authority.  Deletion
        itself is checked, and ambiguity poisons the local pairing store.
        """

        key = self._import_validation_key(pair_id)
        async with self._global_lock:
            self.assert_safe()
            try:
                await self._storage.delete(key)
                remaining = await self._storage.get(key)
            except Exception as exc:
                self._mark_unsafe()
                raise MeshPairingStoreError(
                    "io_ambiguous", "Mesh import validation clear is unsafe"
                ) from exc
            if remaining is not None:
                self._mark_unsafe()
                _fail(
                    "readback_mismatch",
                    "Mesh import validation clear failed read-back verification",
                )

    def _target_activation_receipt_key(self, pair_id: str) -> str:
        return f"{self._prefix}target_activation_receipts/{self._pair_token(pair_id)}.json"

    async def get_target_activation_receipt(
        self, pair_id: str
    ) -> Optional[SignedTargetActivationReceipt]:
        data = await self._get_json(self._target_activation_receipt_key(pair_id))
        if data is None:
            return None
        signed = SignedTargetActivationReceipt.from_dict(data)
        if signed.receipt.authority.pair_id != pair_id:
            _fail("corrupt_state", "target activation receipt key does not match its record")
        return signed

    async def put_target_activation_receipt(
        self, signed: SignedTargetActivationReceipt
    ) -> None:
        """Persist one immutable, target-signed terminal activation proof."""

        if not isinstance(signed, SignedTargetActivationReceipt):
            _fail("invalid_activation_receipt", "invalid target activation receipt")
        async with self._global_lock:
            receipt = signed.receipt
            pair_id = receipt.authority.pair_id
            existing = await self.get_target_activation_receipt(pair_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.receipt.as_dict()
                receipt_data = receipt.as_dict()
                existing_data.pop("activated_at_ms")
                receipt_data.pop("activated_at_ms")
                if existing_data == receipt_data:
                    # A crash after this durable proof but before terminal
                    # session/release work must retain, not rewrite, it.
                    return
                _fail(
                    "activation_receipt_conflict",
                    "target activation receipt is immutable",
                )
            await self._durable_put(
                self._target_activation_receipt_key(pair_id), signed.as_dict()
            )

    async def clear_target_activation_receipt_for_recovery(self, pair_id: str) -> None:
        """Remove a rejected terminal receipt during a strict recovery path.

        This is deliberately not a general receipt-delete API.  The pairing
        service invokes it only after it has either (a) put the target UNSAFE
        and torn its space down for a fresh signed import, or (b) re-proved the
        exact retained e+1 snapshot plus a newly authenticated source e+2
        confirmation.  A receipt that still verifies against the exact local
        e+2 authority is never cleared.
        """

        key = self._target_activation_receipt_key(pair_id)
        async with self._global_lock:
            self.assert_safe()
            try:
                await self._storage.delete(key)
                remaining = await self._storage.get(key)
            except Exception as exc:
                self._mark_unsafe()
                raise MeshPairingStoreError(
                    "io_ambiguous", "Mesh activation receipt clear is unsafe"
                ) from exc
            if remaining is not None:
                self._mark_unsafe()
                _fail(
                    "readback_mismatch",
                    "Mesh activation receipt clear failed read-back verification",
                )

    async def clear_target_activation_receipt_for_resync(self, pair_id: str) -> None:
        """Compatibility spelling for the resync-specific recovery caller."""

        await self.clear_target_activation_receipt_for_recovery(pair_id)

    # -- source terminal activation receipts -----------------------------

    def _source_terminal_disposition_key(self, pair_id: str) -> str:
        return (
            f"{self._prefix}source_terminal_dispositions/"
            f"{self._pair_token(pair_id)}.json"
        )

    async def get_source_terminal_disposition(
        self, pair_id: str
    ) -> Optional[SignedSourceTerminalDispositionReceipt]:
        """Return the immutable source-signed target-release disposition."""

        data = await self._get_json(self._source_terminal_disposition_key(pair_id))
        if data is None:
            return None
        signed = SignedSourceTerminalDispositionReceipt.from_dict(data)
        if signed.receipt.pair_id != pair_id:
            _fail(
                "corrupt_state",
                "source terminal disposition key does not match its record",
            )
        return signed

    async def put_source_terminal_disposition(
        self, signed: SignedSourceTerminalDispositionReceipt
    ) -> None:
        """Persist one source-signed proof permitting target abandonment.

        The source writes this before its mutable session becomes terminal.  A
        retry can observe the same disposition with a different observation
        timestamp; that is not a second authority and must retain the first
        durable bytes rather than turn the record into a conflict.
        """

        if not isinstance(signed, SignedSourceTerminalDispositionReceipt):
            _fail(
                "invalid_terminal_disposition",
                "invalid source terminal disposition",
            )
        async with self._global_lock:
            pair_id = signed.receipt.pair_id
            existing = await self.get_source_terminal_disposition(pair_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.receipt.as_dict()
                receipt_data = signed.receipt.as_dict()
                existing_data.pop("issued_at_ms")
                receipt_data.pop("issued_at_ms")
                if existing_data == receipt_data:
                    return
                _fail(
                    "terminal_disposition_conflict",
                    "source terminal disposition is immutable",
                )
            await self._durable_put(
                self._source_terminal_disposition_key(pair_id), signed.as_dict()
            )

    def _source_pending_eviction_intent_key(self, pair_id: str) -> str:
        return (
            f"{self._prefix}source_pending_eviction_intents/"
            f"{self._pair_token(pair_id)}.json"
        )

    async def get_source_pending_eviction_intent(
        self, pair_id: str
    ) -> Optional[SignedSourcePendingEvictionIntent]:
        data = await self._get_json(self._source_pending_eviction_intent_key(pair_id))
        if data is None:
            return None
        signed = SignedSourcePendingEvictionIntent.from_dict(data)
        if signed.intent.pair_id != pair_id:
            _fail(
                "corrupt_state",
                "source pending eviction intent key does not match its record",
            )
        return signed

    async def put_source_pending_eviction_intent(
        self, signed: SignedSourcePendingEvictionIntent
    ) -> None:
        """Persist the immutable source authorization before PENDING removal."""

        if not isinstance(signed, SignedSourcePendingEvictionIntent):
            _fail(
                "invalid_pending_eviction_intent",
                "invalid source pending eviction intent",
            )
        async with self._global_lock:
            pair_id = signed.intent.pair_id
            existing = await self.get_source_pending_eviction_intent(pair_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.intent.as_dict()
                intent_data = signed.intent.as_dict()
                existing_data.pop("issued_at_ms")
                intent_data.pop("issued_at_ms")
                if existing_data == intent_data:
                    return
                _fail(
                    "pending_eviction_intent_conflict",
                    "source pending eviction intent is immutable",
                )
            await self._durable_put(
                self._source_pending_eviction_intent_key(pair_id), signed.as_dict()
            )

    def _source_preclaim_cancel_barrier_key(self, pair_id: str) -> str:
        return (
            f"{self._prefix}source_preclaim_cancel_barriers/"
            f"{self._pair_token(pair_id)}.json"
        )

    async def get_source_preclaim_cancel_barrier(
        self, pair_id: str
    ) -> Optional[SignedSourcePreClaimCancelBarrier]:
        data = await self._get_json(self._source_preclaim_cancel_barrier_key(pair_id))
        if data is None:
            return None
        signed = SignedSourcePreClaimCancelBarrier.from_dict(data)
        if signed.barrier.pair_id != pair_id:
            _fail(
                "corrupt_state",
                "source pre-claim cancellation barrier key does not match its record",
            )
        return signed

    async def put_source_preclaim_cancel_barrier(
        self, signed: SignedSourcePreClaimCancelBarrier
    ) -> None:
        """Persist the immutable source abort before an ISSUED session changes."""

        if not isinstance(signed, SignedSourcePreClaimCancelBarrier):
            _fail(
                "invalid_preclaim_cancel_barrier",
                "invalid source pre-claim cancellation barrier",
            )
        async with self._global_lock:
            pair_id = signed.barrier.pair_id
            existing = await self.get_source_preclaim_cancel_barrier(pair_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.barrier.as_dict()
                barrier_data = signed.barrier.as_dict()
                existing_data.pop("issued_at_ms")
                barrier_data.pop("issued_at_ms")
                if existing_data == barrier_data:
                    return
                _fail(
                    "preclaim_cancel_barrier_conflict",
                    "source pre-claim cancellation barrier is immutable",
                )
            await self._durable_put(
                self._source_preclaim_cancel_barrier_key(pair_id), signed.as_dict()
            )

    def _source_activation_receipt_key(self, pair_id: str) -> str:
        return f"{self._prefix}source_activation_receipts/{self._pair_token(pair_id)}.json"

    async def get_source_activation_receipt(
        self, pair_id: str
    ) -> Optional[SignedSourceActivationReceipt]:
        data = await self._get_json(self._source_activation_receipt_key(pair_id))
        if data is None:
            return None
        signed = SignedSourceActivationReceipt.from_dict(data)
        if signed.receipt.pair_id != pair_id:
            _fail("corrupt_state", "source activation receipt key does not match its record")
        return signed

    async def put_source_activation_receipt(
        self, signed: SignedSourceActivationReceipt
    ) -> None:
        """Persist one immutable source-signed all-ACK completion proof.

        Both peers persist the exact same signed record: the source obtains a
        retry-safe terminal proof, and the target records the evidence that
        authorizes release of its final ordinary-write reservation.
        """

        if not isinstance(signed, SignedSourceActivationReceipt):
            _fail(
                "invalid_source_activation_receipt",
                "invalid source activation receipt",
            )
        async with self._global_lock:
            receipt = signed.receipt
            pair_id = receipt.pair_id
            existing = await self.get_source_activation_receipt(pair_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.receipt.as_dict()
                receipt_data = receipt.as_dict()
                existing_data.pop("confirmed_at_ms")
                receipt_data.pop("confirmed_at_ms")
                if existing_data == receipt_data:
                    return
                _fail(
                    "source_activation_receipt_conflict",
                    "source activation receipt is immutable",
                )
            await self._durable_put(
                self._source_activation_receipt_key(pair_id), signed.as_dict()
            )

    async def restore_source_activation_receipt(
        self, signed: SignedSourceActivationReceipt
    ) -> None:
        """Replace only a timestamp-variant receipt with target-confirmed bytes.

        ``confirmed_at_ms`` is an observation timestamp, not activation
        authority.  A source can lose its local receipt after the target already
        retained a semantically identical signed copy and minted the terminal
        confirmation against those exact bytes.  The caller has independently
        verified that target-signed confirmation; this narrow helper makes the
        local source retain the exact target-confirmed bytes.  Any substantive
        difference remains immutable-state corruption and fails closed.
        """

        if not isinstance(signed, SignedSourceActivationReceipt):
            _fail(
                "invalid_source_activation_receipt",
                "invalid source activation receipt",
            )
        async with self._global_lock:
            pair_id = signed.receipt.pair_id
            existing = await self.get_source_activation_receipt(pair_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.receipt.as_dict()
                receipt_data = signed.receipt.as_dict()
                existing_data.pop("confirmed_at_ms")
                receipt_data.pop("confirmed_at_ms")
                if existing_data != receipt_data:
                    _fail(
                        "source_activation_receipt_conflict",
                        "source activation receipt is immutable",
                    )
            await self._durable_put(
                self._source_activation_receipt_key(pair_id), signed.as_dict()
            )

    # -- target terminal confirmations -----------------------------------

    def _target_terminal_confirmation_key(self, pair_id: str) -> str:
        return (
            f"{self._prefix}target_terminal_confirmations/"
            f"{self._pair_token(pair_id)}.json"
        )

    async def get_target_terminal_confirmation(
        self, pair_id: str
    ) -> Optional[SignedTargetTerminalConfirmationReceipt]:
        data = await self._get_json(self._target_terminal_confirmation_key(pair_id))
        if data is None:
            return None
        signed = SignedTargetTerminalConfirmationReceipt.from_dict(data)
        if signed.receipt.pair_id != pair_id:
            _fail(
                "corrupt_state",
                "target terminal confirmation key does not match its record",
            )
        return signed

    async def put_target_terminal_confirmation(
        self, signed: SignedTargetTerminalConfirmationReceipt
    ) -> None:
        """Persist the target-signed readback that releases both final fences."""

        if not isinstance(signed, SignedTargetTerminalConfirmationReceipt):
            _fail(
                "invalid_terminal_confirmation",
                "invalid target terminal confirmation",
            )
        async with self._global_lock:
            pair_id = signed.receipt.pair_id
            existing = await self.get_target_terminal_confirmation(pair_id)
            if existing == signed:
                return
            if existing is not None:
                existing_data = existing.receipt.as_dict()
                confirmation_data = signed.receipt.as_dict()
                existing_data.pop("confirmed_at_ms")
                confirmation_data.pop("confirmed_at_ms")
                if existing_data == confirmation_data:
                    return
                _fail(
                    "terminal_confirmation_conflict",
                    "target terminal confirmation is immutable",
                )
            await self._durable_put(
                self._target_terminal_confirmation_key(pair_id), signed.as_dict()
            )

    # -- target acceptance intent ----------------------------------------

    def _target_acceptance_intent_key(self, pair_id: str) -> str:
        return f"{self._prefix}target_acceptance_intents/{self._pair_token(pair_id)}.json"

    async def get_target_acceptance_intent(self, pair_id: str) -> Optional[dict]:
        """Return the immutable pre-reservation target identity record."""

        data = await self._get_json(self._target_acceptance_intent_key(pair_id))
        if data is None:
            return None
        required = {
            "invitation_digest",
            "pair_id",
            "requested_scopes",
            "source_fingerprint",
            "space_id",
            "target_fingerprint",
        }
        if (
            set(data) != required
            or data.get("pair_id") != pair_id
            or type(data.get("space_id")) is not str
            or _SPACE_ID_RE.fullmatch(data["space_id"]) is None
            or type(data.get("source_fingerprint")) is not str
            or type(data.get("target_fingerprint")) is not str
            or type(data.get("invitation_digest")) is not str
            or type(data.get("requested_scopes")) is not list
            or any(type(scope) is not str for scope in data["requested_scopes"])
        ):
            _fail("corrupt_state", "target acceptance intent is corrupt")
        return data

    async def put_target_acceptance_intent(self, pair_id: str, intent: dict) -> None:
        """Create one exact pair-id-to-target-space binding before reservation."""

        if type(intent) is not dict:
            _fail("invalid_acceptance_intent", "invalid target acceptance intent")
        async with self._global_lock:
            existing = await self.get_target_acceptance_intent(pair_id)
            if existing == intent:
                return
            if existing is not None:
                _fail(
                    "acceptance_conflict",
                    "pair id is already bound to another target acceptance",
                )
            # Validate through the same strict reader before making it durable.
            required = {
                "invitation_digest",
                "pair_id",
                "requested_scopes",
                "source_fingerprint",
                "space_id",
                "target_fingerprint",
            }
            if (
                set(intent) != required
                or intent.get("pair_id") != pair_id
                or type(intent.get("space_id")) is not str
                or _SPACE_ID_RE.fullmatch(intent["space_id"]) is None
                or type(intent.get("source_fingerprint")) is not str
                or type(intent.get("target_fingerprint")) is not str
                or type(intent.get("invitation_digest")) is not str
                or type(intent.get("requested_scopes")) is not list
                or any(type(scope) is not str for scope in intent["requested_scopes"])
            ):
                _fail("invalid_acceptance_intent", "invalid target acceptance intent")
            await self._durable_put(self._target_acceptance_intent_key(pair_id), intent)

    def _target_pairing_admission_anchor_migration_key(self) -> str:
        return f"{self._prefix}target_pairing_admission_anchor_migrations/v1.json"

    async def target_pairing_admission_anchor_migration_complete(self) -> bool:
        """Whether retained #417 intent provenance has been indexed at startup."""

        data = await self._get_json(self._target_pairing_admission_anchor_migration_key())
        if data is None:
            return False
        if data != {"protocol_version": 1}:
            _fail(
                "corrupt_state",
                "target pairing admission-anchor migration marker is invalid",
            )
        return True

    async def mark_target_pairing_admission_anchor_migration_complete(self) -> None:
        """Durably mark a fully completed, bounded startup backfill."""

        async with self._global_lock:
            if await self.target_pairing_admission_anchor_migration_complete():
                return
            await self._durable_put(
                self._target_pairing_admission_anchor_migration_key(),
                {"protocol_version": 1},
            )

    async def list_target_acceptance_intents_for_migration(
        self,
        *,
        max_intents: int = MAX_TARGET_ACCEPTANCE_INTENTS_MIGRATION,
    ) -> list[tuple[str, dict]]:
        """Read the bounded legacy #417 intent set for one startup migration.

        This is intentionally not an ordinary-write API: normal guards use
        only the direct per-space anchor/fence keys.  A cap+one listing keeps a
        hostile or unexpectedly large retained inventory from being silently
        accepted as a partial migration.
        """

        if type(max_intents) is not int or max_intents <= 0:
            _fail("invalid_limit", "invalid target acceptance migration limit")
        prefix = f"{self._prefix}target_acceptance_intents/"
        try:
            objects = await self._storage.list_objects(prefix, max_keys=max_intents + 1)
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError(
                "io_ambiguous", "target acceptance migration listing is unsafe"
            ) from exc
        if len(objects) > max_intents:
            _fail(
                "too_many_acceptance_intents",
                "target acceptance migration inventory exceeds its bound",
            )
        intents: list[tuple[str, dict]] = []
        for obj in objects:
            key = obj.get("Key") if isinstance(obj, dict) else None
            size = obj.get("Size") if isinstance(obj, dict) else None
            if (
                not isinstance(key, str)
                or not key.endswith(".json")
                or type(size) is not int
                or size < 0
                or size > MAX_PAIRING_RECORD_BYTES
            ):
                _fail(
                    "corrupt_state",
                    "target acceptance migration namespace contains an invalid entry",
                )
            data = await self._get_json(key)
            if data is None:
                _fail(
                    "io_ambiguous",
                    "target acceptance intent disappeared during migration",
                )
            pair_id = data.get("pair_id")
            if type(pair_id) is not str or _PAIR_ID_RE.fullmatch(pair_id) is None:
                _fail("corrupt_state", "target acceptance migration record has invalid pair id")
            if key != self._target_acceptance_intent_key(pair_id):
                _fail("corrupt_state", "target acceptance intent key does not match its pair id")
            # Reuse the closed reader validation rather than duplicating its
            # field contract here.
            intent = await self.get_target_acceptance_intent(pair_id)
            if intent is None:
                _fail(
                    "io_ambiguous",
                    "target acceptance intent disappeared during migration",
                )
            intents.append((pair_id, intent))
        return intents

    # -- reservations ------------------------------------------------------

    def _target_pairing_admission_anchor_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            _fail("invalid_space_id", "invalid space id")
        return f"{self._prefix}target_pairing_admission_anchors/{space_id}.json"

    async def get_target_pairing_admission_anchor(
        self, space_id: str
    ) -> Optional[SignedTargetPairingAdmissionAnchor]:
        """Return the permanent signed #417 discriminator for one target space."""

        data = await self._get_json(self._target_pairing_admission_anchor_key(space_id))
        if data is None:
            return None
        signed = SignedTargetPairingAdmissionAnchor.from_dict(data)
        if signed.anchor.space_id != space_id:
            _fail(
                "corrupt_state",
                "target pairing admission anchor key does not match its record",
            )
        return signed

    async def put_target_pairing_admission_anchor(
        self, signed: SignedTargetPairingAdmissionAnchor
    ) -> None:
        """Persist the immutable per-space target #417 protocol discriminator.

        Unlike the operational fence, this record never changes phase or owner.
        It only prevents a target that has once participated in #417 from
        falling back to a legacy ordinary-write decision when mutable tail
        records are independently lost.
        """

        if not isinstance(signed, SignedTargetPairingAdmissionAnchor):
            _fail(
                "invalid_target_pairing_anchor",
                "invalid target pairing admission anchor",
            )
        anchor = signed.anchor
        try:
            signed.verify(anchor.target_public_key)
        except Exception as exc:
            _fail(
                "invalid_target_pairing_anchor",
                "target pairing admission anchor signature is invalid",
            )
            raise AssertionError("unreachable") from exc
        async with self._global_lock:
            existing = await self.get_target_pairing_admission_anchor(anchor.space_id)
            if existing is not None:
                try:
                    existing.verify(existing.anchor.target_public_key)
                except Exception as exc:
                    _fail(
                        "corrupt_state",
                        "target pairing admission anchor signature is invalid",
                    )
                    raise AssertionError("unreachable") from exc
                if (
                    existing.anchor.space_id == anchor.space_id
                    and existing.anchor.target_public_key == anchor.target_public_key
                    and existing.anchor.target_fingerprint
                    == anchor.target_fingerprint
                ):
                    return
                _fail(
                    "target_pairing_protocol_conflict",
                    "target pairing admission anchor is immutable",
                )
            await self._durable_put(
                self._target_pairing_admission_anchor_key(anchor.space_id),
                signed.as_dict(),
            )

    def _target_pairing_protocol_floor_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            _fail("invalid_space_id", "invalid space id")
        return f"{self._prefix}target_pairing_protocol_floors/{space_id}.json"

    def _target_pairing_fence_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            _fail("invalid_space_id", "invalid space id")
        return f"{self._prefix}target_pairing_fences/{space_id}.json"

    def _target_pairing_current_tail_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            _fail("invalid_space_id", "invalid space id")
        return f"{self._prefix}target_pairing_current_tails/{space_id}.json"

    async def get_target_pairing_protocol_floor(
        self, space_id: str
    ) -> Optional[SignedTargetPairingFenceAuthority]:
        """Return the immutable per-space #417 target-protocol discriminator."""

        data = await self._get_json(self._target_pairing_protocol_floor_key(space_id))
        if data is None:
            return None
        signed = SignedTargetPairingFenceAuthority.from_dict(data)
        if signed.authority.space_id != space_id:
            _fail(
                "corrupt_state",
                "target pairing protocol floor key does not match its record",
            )
        return signed

    async def put_target_pairing_protocol_floor(
        self, signed: SignedTargetPairingFenceAuthority
    ) -> None:
        """Persist the first target-signed #417 discriminator for ``space_id``.

        A target fence is intentionally not allowed to disappear into the
        legacy compatibility branch once this local instance has accepted a
        #417 target invitation.  The floor carries no ordinary-write
        permission; it merely makes loss of the current direct fence fail
        closed.
        """

        if not isinstance(signed, SignedTargetPairingFenceAuthority):
            _fail("invalid_target_pairing_fence", "invalid target pairing floor")
        authority = signed.authority
        try:
            signed.verify(authority.target_public_key)
        except Exception as exc:
            _fail("invalid_target_pairing_fence", "target pairing floor signature is invalid")
            raise AssertionError("unreachable") from exc
        async with self._global_lock:
            existing = await self.get_target_pairing_protocol_floor(
                authority.space_id
            )
            if existing is not None:
                try:
                    existing.verify(existing.authority.target_public_key)
                except Exception as exc:
                    _fail(
                        "corrupt_state",
                        "target pairing protocol floor signature is invalid",
                    )
                    raise AssertionError("unreachable") from exc
                current = existing.authority
                if (
                    current.space_id == authority.space_id
                    and current.target_fingerprint == authority.target_fingerprint
                    and current.target_public_key == authority.target_public_key
                ):
                    return
                _fail(
                    "target_pairing_protocol_conflict",
                    "target pairing protocol floor is immutable",
                )
            await self._durable_put(
                self._target_pairing_protocol_floor_key(authority.space_id),
                signed.as_dict(),
            )

    async def _get_target_pairing_authority(
        self, key: str, space_id: str, *, label: str
    ) -> Optional[SignedTargetPairingFenceAuthority]:
        """Read one direct signed target-tail authority by its exact key."""

        data = await self._get_json(key)
        if data is None:
            return None
        signed = SignedTargetPairingFenceAuthority.from_dict(data)
        if signed.authority.space_id != space_id:
            _fail("corrupt_state", f"target pairing {label} key does not match its record")
        return signed

    async def get_target_pairing_fence(
        self, space_id: str
    ) -> Optional[SignedTargetPairingFenceAuthority]:
        """Return the bounded operational target fence for exactly one space."""

        return await self._get_target_pairing_authority(
            self._target_pairing_fence_key(space_id), space_id, label="fence"
        )

    async def get_target_pairing_current_tail(
        self, space_id: str
    ) -> Optional[SignedTargetPairingFenceAuthority]:
        """Return the signed current-generation index for exactly one target space.

        This independent copy of the current tail prevents a replay of a
        historically released operational fence from masking a newer held tail
        after its raw reservation is lost.  It is not a session-history scan.
        """

        return await self._get_target_pairing_authority(
            self._target_pairing_current_tail_key(space_id),
            space_id,
            label="current tail",
        )

    @staticmethod
    def _target_pairing_authority_timestamp_retry(current, incoming) -> bool:
        """Whether two authorities differ only in local observation time."""

        current_data = current.as_dict()
        incoming_data = incoming.as_dict()
        current_data.pop("issued_at_ms")
        incoming_data.pop("issued_at_ms")
        return current_data == incoming_data

    def _validate_target_pairing_authority_transition(
        self,
        current: SignedTargetPairingFenceAuthority,
        incoming: SignedTargetPairingFenceAuthority,
        *,
        replace_settled: bool,
        rearm_recovery: bool,
        replace_timestamp_variant: bool,
    ) -> bool:
        """Validate one direct target-tail record transition.

        Returns ``True`` for an exact semantic retry whose original signed
        bytes must remain authoritative; otherwise raises or returns ``False``
        for a durable successor write.
        """

        existing = current.authority
        authority = incoming.authority
        if (
            existing.target_fingerprint != authority.target_fingerprint
            or existing.target_public_key != authority.target_public_key
        ):
            _fail(
                "target_pairing_fence_conflict",
                "target pairing fence target identity is immutable",
            )
        if existing.pair_id == authority.pair_id:
            if (
                existing.phase == authority.phase
                and self._target_pairing_authority_timestamp_retry(existing, authority)
            ):
                # A restart can reconstruct the same prefix with a later local
                # observation timestamp. Keep the first signed bytes; they are
                # the authority used by both direct target-tail records.
                return not replace_timestamp_variant
            if (
                existing.phase == "terminal_confirmed"
                and authority.phase == "held"
                and rearm_recovery
            ):
                return False
            if existing.phase == "held" and authority.phase in (
                "terminal_confirmed",
                "released",
            ):
                return False
            _fail(
                "target_pairing_fence_conflict",
                "target pairing fence has an invalid same-pair transition",
            )
        if (
            not replace_settled
            or existing.phase != "released"
            or authority.phase != "held"
        ):
            _fail(
                "target_pairing_fence_conflict",
                "another target pairing fence owns this space",
            )
        return False

    async def _put_target_pairing_authority(
        self,
        signed: SignedTargetPairingFenceAuthority,
        *,
        key: str,
        label: str,
        replace_settled: bool,
        rearm_recovery: bool,
        replace_timestamp_variant: bool = False,
    ) -> None:
        """Write one signed direct target-tail authority with strict transitions."""

        if not isinstance(signed, SignedTargetPairingFenceAuthority):
            _fail("invalid_target_pairing_fence", f"invalid target pairing {label}")
        authority = signed.authority
        try:
            signed.verify(authority.target_public_key)
        except Exception as exc:
            _fail(
                "invalid_target_pairing_fence",
                f"target pairing {label} signature is invalid",
            )
            raise AssertionError("unreachable") from exc
        async with self._global_lock:
            existing = await self._get_target_pairing_authority(
                key, authority.space_id, label=label
            )
            if existing == signed:
                return
            if existing is not None:
                try:
                    existing.verify(existing.authority.target_public_key)
                except Exception as exc:
                    _fail(
                        "corrupt_state",
                        f"target pairing {label} signature is invalid",
                    )
                    raise AssertionError("unreachable") from exc
                if self._validate_target_pairing_authority_transition(
                    existing,
                    signed,
                    replace_settled=replace_settled,
                    rearm_recovery=rearm_recovery,
                    replace_timestamp_variant=replace_timestamp_variant,
                ):
                    return
            await self._durable_put(key, signed.as_dict())

    async def put_target_pairing_fence(
        self,
        signed: SignedTargetPairingFenceAuthority,
        *,
        replace_settled: bool = False,
        rearm_recovery: bool = False,
    ) -> None:
        """Persist an exact signed target fence transition with readback.

        The service holds the pair then space locks while it invokes this
        method.  The store nevertheless enforces the small monotonic state
        machine so an accidental local caller cannot replace a held tail with
        another pair.  ``replace_settled`` is reserved for a fresh blank-target
        acceptance after a prior *released* owner has been proven safe.
        ``rearm_recovery`` is narrower: it changes the SAME pair from a signed
        terminal proof back to ``held`` immediately before an UNSAFE recovery
        path recreates its raw reservation.
        """

        await self._put_target_pairing_authority(
            signed,
            key=self._target_pairing_fence_key(signed.authority.space_id),
            label="fence",
            replace_settled=replace_settled,
            rearm_recovery=rearm_recovery,
        )

    async def put_target_pairing_current_tail(
        self,
        signed: SignedTargetPairingFenceAuthority,
        *,
        replace_settled: bool = False,
        rearm_recovery: bool = False,
        reconcile_fence_bytes: bool = False,
    ) -> None:
        """Advance the signed per-space current-tail index with readback.

        It shares the operational fence's transition graph but is kept at a
        separate direct key. Ordinary-write authorization requires both keys
        to bind the same tail, so replaying an old released fence alone can
        never authorize a newer incomplete target pairing.
        """

        await self._put_target_pairing_authority(
            signed,
            key=self._target_pairing_current_tail_key(signed.authority.space_id),
            label="current tail",
            replace_settled=replace_settled,
            rearm_recovery=rearm_recovery,
            replace_timestamp_variant=reconcile_fence_bytes,
        )

    def _reservation_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            raise MeshPairingStoreError("invalid_space_id", "invalid space id")
        return f"{self._prefix}reservations/{space_id}.json"

    async def _ensure_reserved_index(self) -> dict[str, str]:
        if self._reserved is not None:
            return self._reserved
        async with self._reserved_load_lock:
            if self._reserved is not None:
                return self._reserved
            return await self._load_reserved_index()

    async def _load_reserved_index(self) -> dict[str, str]:
        """Hydrate once while ``_reserved_load_lock`` excludes stale loaders."""

        self.assert_safe()
        prefix = f"{self._prefix}reservations/"
        try:
            objects = await self._storage.list_objects(
                prefix, max_keys=MAX_PAIRING_RESERVATIONS + 1
            )
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError("io_ambiguous", "Mesh reservation load is unsafe") from exc
        if len(objects) > MAX_PAIRING_RESERVATIONS:
            self._mark_unsafe()
            _fail(
                "too_many_reservations",
                "Mesh reservation inventory exceeds its bound",
            )
        index: dict[str, str] = {}
        for obj in objects:
            key = obj.get("Key") if isinstance(obj, dict) else None
            size = obj.get("Size") if isinstance(obj, dict) else None
            if (
                not isinstance(key, str)
                or not key.endswith(".json")
                or type(size) is not int
                or size < 0
                or size > MAX_PAIRING_RECORD_BYTES
            ):
                self._mark_unsafe()
                _fail(
                    "corrupt_state",
                    "Mesh reservation namespace contains an invalid entry",
                )
            try:
                data = await self._get_json(key)
            except MeshPairingStoreError:
                self._mark_unsafe()
                raise
            except Exception as exc:
                self._mark_unsafe()
                raise MeshPairingStoreError(
                    "corrupt_state", "Mesh reservation record is unreadable"
                ) from exc
            if data is None:
                self._mark_unsafe()
                _fail("corrupt_state", "Mesh reservation record is unreadable")
            if set(data) != {"space_id", "pair_id", "created_at_ms"}:
                self._mark_unsafe()
                _fail("corrupt_state", "Mesh reservation record has invalid shape")
            space_id = data.get("space_id")
            pair_id = data.get("pair_id")
            created_at_ms = data.get("created_at_ms")
            if (
                type(space_id) is not str
                or _SPACE_ID_RE.fullmatch(space_id) is None
                or type(pair_id) is not str
                or _PAIR_ID_RE.fullmatch(pair_id) is None
                or type(created_at_ms) is not int
                or created_at_ms < 0
                or self._reservation_key(space_id) != key
            ):
                self._mark_unsafe()
                _fail("corrupt_state", "Mesh reservation record is invalid")
            if space_id in index:
                self._mark_unsafe()
                _fail("corrupt_state", "duplicate Mesh reservation record")
            index[space_id] = pair_id
        self._reserved = index
        return index

    async def reserve(self, space_id: str, pair_id: str, *, now_ms: int) -> None:
        """Reserve ``space_id`` exclusively for ``pair_id``.

        Fails closed if the space is already reserved by a DIFFERENT pairing or
        carries any irreversible source-preparation provenance. Idempotent for
        the same ``pair_id`` (restart-safe re-reserve). Callers MUST hold
        :meth:`space_lock` across a virginity check and this reserve so a
        populated or formerly shared space is never merged (M5).
        """

        if _PAIR_ID_RE.fullmatch(pair_id) is None:
            _fail("invalid_pair_id", "invalid pair id")
        async with self._global_lock:
            preparation = await self.get_source_preparation(space_id)
            if preparation is not None:
                _fail(
                    "space_reserved",
                    "space has Project Mesh source-preparation provenance and "
                    "cannot be reused as a pairing target",
                )
            # The in-memory inventory is only an operational accelerator.  A
            # fresh acceptance can replace a released direct target-tail
            # record, so it must never rely on a stale cache and overwrite a
            # raw reservation that survived a crash or an out-of-band durable
            # change.  Re-read the exact key and fail closed if it disagrees
            # with the cache before making a new reservation visible.
            existing = await self.get_reservation_direct(space_id)
            index = await self._ensure_reserved_index()
            if index.get(space_id) != existing:
                _fail(
                    "reservation_index_mismatch",
                    "Mesh reservation cache disagrees with durable reservation",
                )
            if existing is not None and existing != pair_id:
                _fail("space_reserved", "target space is already reserved by another pairing")
            if existing is None and len(index) >= MAX_PAIRING_RESERVATIONS:
                _fail(
                    "too_many_reservations",
                    "Mesh reservation inventory exceeds its bound",
                )
            await self._durable_put(
                self._reservation_key(space_id),
                {"space_id": space_id, "pair_id": pair_id, "created_at_ms": now_ms},
            )
            index[space_id] = pair_id

    async def release(self, space_id: str, pair_id: str) -> None:
        """Release ``space_id`` iff it is reserved by ``pair_id`` (else no-op)."""

        async with self._global_lock:
            index = await self._ensure_reserved_index()
            if index.get(space_id) != pair_id:
                return
            self.assert_safe()
            try:
                key = self._reservation_key(space_id)
                await self._storage.delete(key)
                observed = await self._storage.get(key)
            except Exception as exc:
                self._mark_unsafe()
                raise MeshPairingStoreError("io_ambiguous", "Mesh reservation release is unsafe") from exc
            if observed is not None:
                self._mark_unsafe()
                _fail(
                    "readback_mismatch",
                    "Mesh reservation release failed read-back verification",
                )
            index.pop(space_id, None)

    async def get_reservation(self, space_id: str) -> Optional[str]:
        async with self._global_lock:
            index = await self._ensure_reserved_index()
            return index.get(space_id)

    async def get_reservation_direct(self, space_id: str) -> Optional[str]:
        """Read one reservation key without hydrating the global index.

        This is intentionally the ordinary-write guard path.  The reservation
        inventory is an operational convenience for acceptance/recovery, not
        authority for unrelated writes; a cold write must not enumerate every
        reserved space or trip the inventory ceiling.
        """

        data = await self._get_json(self._reservation_key(space_id))
        if data is None:
            return None
        if set(data) != {"space_id", "pair_id", "created_at_ms"}:
            _fail("corrupt_state", "Mesh reservation record has invalid shape")
        pair_id = data.get("pair_id")
        created_at_ms = data.get("created_at_ms")
        if (
            data.get("space_id") != space_id
            or type(pair_id) is not str
            or _PAIR_ID_RE.fullmatch(pair_id) is None
            or type(created_at_ms) is not int
            or created_at_ms < 0
        ):
            _fail("corrupt_state", "Mesh reservation record is invalid")
        return pair_id

    async def find_reservations_by_pair_id(self, pair_id: str) -> tuple[str, ...]:
        """Return every target reservation held by ``pair_id``.

        New #417 records create an immutable acceptance intent before their
        single reservation, but old versions could write two target
        reservations for the same caller-controlled pair id. Preserve that
        observable legacy collision rather than poisoning the whole local
        store: the service can refuse new reuse and let an operator recover a
        verified bare reservation without touching the separately bound one.
        """

        if _PAIR_ID_RE.fullmatch(pair_id) is None:
            _fail("invalid_pair_id", "invalid pair id")
        async with self._global_lock:
            index = await self._ensure_reserved_index()
            return tuple(
                sorted(space_id for space_id, value in index.items() if value == pair_id)
            )

    async def find_reservation_by_pair_id(self, pair_id: str) -> Optional[str]:
        """Return the sole reservation for ``pair_id`` when it is unambiguous.

        Callers that need migration handling should use
        :meth:`find_reservations_by_pair_id`; this convenience method retains
        the prior one-or-none contract without treating an old collision as
        process-wide storage corruption.
        """

        spaces = await self.find_reservations_by_pair_id(pair_id)
        if len(spaces) > 1:
            _fail(
                "reservation_collision",
                "pair id has multiple legacy target reservations",
            )
        return spaces[0] if spaces else None

    async def assert_space_not_reserved(self, space_id: str) -> None:
        """Raise ``MeshPairingStoreError`` if ``space_id`` is reserved (guard hook).

        The source-preparation record is always read durably.  For target
        reservations, a hydrated negative in-memory index remains a fast path.
        A process-poisoned store refuses rather than silently admitting a
        write. Because preparation evidence shares one fingerprint-neutral
        namespace, an unreadable/ambiguous local store may conservatively
        refuse unrelated spaces until process recovery.
        """

        # Source-preparation state lives under a stable, fingerprint-independent
        # key and is ALWAYS read durably.  A cached target-reservation miss from
        # this or another serving process must never hide a newly persisted
        # PREPARING fence.
        preparation = await self.get_source_preparation(space_id)
        if (
            preparation is not None
            and preparation.state_enum is SourcePreparationState.PREPARING
        ):
            _fail(
                "space_reserved",
                "space is reserved for Project Mesh source preparation",
            )

        # This guard deliberately reads a single durable key.  A cold ordinary
        # write must not hydrate/list the global reservation inventory: that
        # inventory is bounded operational state and may contain unrelated
        # pairings.  A stale cached miss must never hide a reservation written
        # by another serving process.
        reserved_by = await self.get_reservation_direct(space_id)
        if reserved_by is not None:
            _fail("space_reserved", "space is reserved for a Project Mesh pairing")

    async def assert_direct_local_allowed(self, space_id: str) -> None:
        """Refuse DIRECT_LOCAL whenever durable source provenance exists.

        This always reads the fingerprint-neutral intent key. COMPLETE is not
        an ordinary-write reservation because a healthy source must use STAGED,
        but it permanently prevents downgrade to local authority after loss or
        restore of the Hivemind prefix.
        """

        preparation = await self.get_source_preparation(space_id)
        if preparation is not None:
            _fail(
                "direct_local_forbidden",
                "direct-local access is forbidden for a Project Mesh source",
            )

    # -- source activation fences ----------------------------------------

    def _activation_fence_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            _fail("invalid_space_id", "invalid space id")
        return f"{self._prefix}activation_fences/{space_id}.json"

    async def get_activation_fence_record(
        self, space_id: str
    ) -> Optional[tuple[str, str]]:
        data = await self._get_json(self._activation_fence_key(space_id))
        if data is None:
            return None
        fields = set(data)
        if fields not in (
            {"space_id", "pair_id", "created_at_ms"},
            {"space_id", "pair_id", "created_at_ms", "phase"},
        ):
            _fail("corrupt_state", "Mesh activation fence has invalid shape")
        pair_id = data.get("pair_id")
        created_at_ms = data.get("created_at_ms")
        phase = data.get("phase", "activation")
        if (
            data.get("space_id") != space_id
            or type(pair_id) is not str
            or _PAIR_ID_RE.fullmatch(pair_id) is None
            or type(created_at_ms) is not int
            or created_at_ms < 0
            or type(phase) is not str
            or phase not in _ACTIVATION_FENCE_PHASES
        ):
            _fail("corrupt_state", "Mesh activation fence is invalid")
        return pair_id, phase

    async def get_activation_fence(self, space_id: str) -> Optional[str]:
        record = await self.get_activation_fence_record(space_id)
        return None if record is None else record[0]

    async def put_activation_fence(
        self,
        space_id: str,
        pair_id: str,
        *,
        now_ms: int,
        phase: str = "activation",
    ) -> None:
        if _PAIR_ID_RE.fullmatch(pair_id) is None:
            _fail("invalid_pair_id", "invalid pair id")
        if type(now_ms) is not int or now_ms < 0:
            _fail("invalid_timestamp", "invalid activation fence timestamp")
        if type(phase) is not str or phase not in _ACTIVATION_FENCE_PHASES:
            _fail("invalid_activation_phase", "invalid Mesh activation fence phase")
        async with self._global_lock:
            existing = await self.get_activation_fence_record(space_id)
            if existing is not None and existing[0] != pair_id:
                _fail(
                    "activation_conflict",
                    "another Mesh activation fence owns this space",
                )
            if existing is not None and existing[1] == phase:
                return
            if existing is not None and (
                existing[1] != "activation" or phase != "source_terminal_confirmation"
            ):
                _fail(
                    "invalid_activation_phase",
                    "Mesh activation fence phase cannot be rewound",
                )
            await self._durable_put(
                self._activation_fence_key(space_id),
                {
                    "space_id": space_id,
                    "pair_id": pair_id,
                    "created_at_ms": now_ms,
                    "phase": phase,
                },
            )

    async def release_activation_fence(self, space_id: str, pair_id: str) -> None:
        async with self._global_lock:
            existing = await self.get_activation_fence_record(space_id)
            if existing is None or existing[0] != pair_id:
                return
            key = self._activation_fence_key(space_id)
            self.assert_safe()
            try:
                await self._storage.delete(key)
                observed = await self._storage.get(key)
            except Exception as exc:
                self._mark_unsafe()
                raise MeshPairingStoreError(
                    "io_ambiguous", "Mesh activation fence release is unsafe"
                ) from exc
            if observed is not None:
                self._mark_unsafe()
                _fail(
                    "delete_unconfirmed",
                    "Mesh activation fence release was not confirmed",
                )

    # -- one-time legacy activation-index migration ----------------------

    def _activation_migration_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            _fail("invalid_space_id", "invalid space id")
        return f"{self._prefix}activation_migrations/{space_id}.json"

    def _source_activation_protocol_floor_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            _fail("invalid_space_id", "invalid space id")
        return f"{self._prefix}source_activation_protocol_floors/{space_id}.json"

    async def get_source_activation_protocol_floor(
        self, space_id: str
    ) -> Optional[SignedSourceActivationMigrationAuthority]:
        """Return the immutable signed #417 source-protocol discriminator."""

        data = await self._get_json(self._source_activation_protocol_floor_key(space_id))
        if data is None:
            return None
        signed = SignedSourceActivationMigrationAuthority.from_dict(data)
        if signed.authority.space_id != space_id:
            _fail(
                "corrupt_state",
                "source activation protocol floor key does not match its record",
            )
        return signed

    async def put_source_activation_protocol_floor(
        self, signed: SignedSourceActivationMigrationAuthority
    ) -> None:
        """Persist the first signed #417 source-tail discriminator per space.

        This record intentionally survives individual tail completion.  It
        prevents a later raw rewrite of a signed active index into a legacy v1
        record from re-enabling compatibility behavior on a source that has
        already entered the #417 authority protocol.
        """

        if not isinstance(signed, SignedSourceActivationMigrationAuthority):
            _fail(
                "invalid_source_activation_migration",
                "invalid source activation protocol floor",
            )
        async with self._global_lock:
            existing = await self.get_source_activation_protocol_floor(
                signed.authority.space_id
            )
            if existing is not None:
                if (
                    existing.authority.space_id == signed.authority.space_id
                    and existing.authority.source_fingerprint
                    == signed.authority.source_fingerprint
                ):
                    return
                _fail(
                    "source_activation_protocol_conflict",
                    "source activation protocol floor is immutable",
                )
            await self._durable_put(
                self._source_activation_protocol_floor_key(
                    signed.authority.space_id
                ),
                signed.as_dict(),
            )

    async def get_activation_migration_entry(
        self, space_id: str
    ) -> Optional[
        tuple[str, bool, SignedSourceActivationMigrationAuthority | None]
    ]:
        """Return ``(owner, source_tail, signed_authority)`` for the index.

        Version 1 records are the original one-time legacy migration result.
        Version 2 records are deliberately re-armed by a #417 source before
        Transition 1.  That provenance distinction is durable: an ACTIVE v2
        owner must retain its signed terminal confirmation before this index can
        be cleared, even if the mutable export evidence, marker, and fence are
        independently lost.
        """

        data = await self._get_json(self._activation_migration_key(space_id))
        if data is None:
            return None
        if not isinstance(data, dict):
            _fail("corrupt_state", "Mesh activation migration has invalid shape")
        version = data.get("protocol_version")
        if version == 1:
            expected_fields = {
                "pair_id",
                "protocol_version",
                "space_id",
                "updated_at_ms",
            }
            source_tail = False
            signed_authority = None
        elif version == 2:
            expected_fields = {
                "authority",
                "kind",
                "pair_id",
                "protocol_version",
                "space_id",
                "updated_at_ms",
            }
            source_tail = True
            try:
                signed_authority = SignedSourceActivationMigrationAuthority.from_dict(
                    data["authority"]
                )
            except Exception as exc:
                _fail(
                    "corrupt_state", "Mesh source activation migration is invalid"
                )
                raise AssertionError("unreachable") from exc
        else:
            _fail("corrupt_state", "Mesh activation migration is invalid")
        if set(data) != expected_fields:
            _fail("corrupt_state", "Mesh activation migration has invalid shape")
        pair_id = data.get("pair_id")
        updated_at_ms = data.get("updated_at_ms")
        if (
            data.get("space_id") != space_id
            or type(pair_id) is not str
            or (pair_id != "" and _PAIR_ID_RE.fullmatch(pair_id) is None)
            or type(updated_at_ms) is not int
            or updated_at_ms < 0
            or (source_tail and (pair_id == "" or data.get("kind") != "source_activation"))
            or (
                source_tail
                and (
                    signed_authority is None
                    or signed_authority.authority.pair_id != pair_id
                    or signed_authority.authority.space_id != space_id
                )
            )
        ):
            _fail("corrupt_state", "Mesh activation migration is invalid")
        return pair_id, source_tail, signed_authority

    async def get_activation_migration(self, space_id: str) -> Optional[str]:
        """Return migration owner, ``''`` for proven-clear, or None if absent."""

        entry = await self.get_activation_migration_entry(space_id)
        return None if entry is None else entry[0]

    async def put_activation_migration(
        self,
        space_id: str,
        pair_id: str,
        *,
        now_ms: int,
        rearm_for_source_activation: bool = False,
        source_activation_authority: SignedSourceActivationMigrationAuthority | None = None,
        replace_settled_source_activation: bool = False,
    ) -> None:
        """Persist the one-time scan result atomically.

        ``pair_id == ''`` means the legacy history was exhaustively proven
        clear. A non-empty owner means exactly that legacy activation was found;
        it may transition only to clear after its terminal session is proven.

        A #417 source approval may explicitly re-arm a previously-clear index
        before Transition 1.  That is not a legacy migration rewrite: it is the
        second durable per-space fence for the current source tail, used only by
        the pairing service after it persisted the exact APPROVED session.
        """

        if type(pair_id) is not str or (
            pair_id != "" and _PAIR_ID_RE.fullmatch(pair_id) is None
        ):
            _fail("invalid_pair_id", "invalid activation migration pair id")
        if rearm_for_source_activation and pair_id == "":
            _fail("invalid_pair_id", "source activation migration needs an owner")
        if rearm_for_source_activation:
            if not isinstance(
                source_activation_authority, SignedSourceActivationMigrationAuthority
            ):
                _fail(
                    "invalid_source_activation_migration",
                    "source activation migration needs signed authority",
                )
            authority = source_activation_authority.authority
            if (
                authority.pair_id != pair_id
                or authority.space_id != space_id
                or not authority.requires_terminal_confirmation
            ):
                _fail(
                    "invalid_source_activation_migration",
                    "source activation migration authority does not bind its key",
                )
        elif source_activation_authority is not None or replace_settled_source_activation:
            _fail(
                "invalid_source_activation_migration",
                "legacy activation migration cannot carry source authority",
            )
        if type(now_ms) is not int or now_ms < 0:
            _fail("invalid_timestamp", "invalid activation migration timestamp")
        async with self._global_lock:
            existing_entry = await self.get_activation_migration_entry(space_id)
            existing = None if existing_entry is None else existing_entry[0]
            existing_source_tail = (
                False if existing_entry is None else existing_entry[1]
            )
            existing_authority = (
                None if existing_entry is None else existing_entry[2]
            )
            if existing == pair_id and (
                not rearm_for_source_activation
                or (
                    existing_source_tail
                    and existing_authority == source_activation_authority
                )
            ):
                return
            if (
                existing == ""
                and pair_id != ""
                and not rearm_for_source_activation
            ):
                _fail(
                    "migration_conflict",
                    "completed Mesh activation migration cannot regain an owner",
                )
            if (
                existing_source_tail
                and existing not in (None, "", pair_id)
                and not (
                    rearm_for_source_activation
                    and replace_settled_source_activation
                )
            ):
                _fail(
                    "migration_conflict",
                    "settled source activation migration cannot change owner",
                )
            if (
                not existing_source_tail
                and existing not in (None, "")
                and pair_id not in ("", existing)
            ):
                _fail(
                    "migration_conflict",
                    "another Mesh activation migration owns this space",
                )
            record = {
                "pair_id": pair_id,
                "protocol_version": 2 if rearm_for_source_activation else 1,
                "space_id": space_id,
                "updated_at_ms": now_ms,
            }
            if rearm_for_source_activation:
                record["kind"] = "source_activation"
                record["authority"] = source_activation_authority.as_dict()
            await self._durable_put(
                self._activation_migration_key(space_id),
                record,
            )

    # -- one-time secret burn ---------------------------------------------

    def _secret_key(self, pair_id: str) -> str:
        return f"{self._prefix}secrets/{self._pair_token(pair_id)}.json"

    def _pair_token(self, pair_id: str) -> str:
        if _PAIR_ID_RE.fullmatch(pair_id) is None:
            raise MeshPairingStoreError("invalid_pair_id", "invalid pair id")
        return pair_id

    async def _get_secret_burn(self, pair_id: str) -> Optional[dict]:
        data = await self._get_json(self._secret_key(pair_id))
        if data is None:
            return None
        if (
            set(data) != {"pair_id", "secret_digest", "burned_at_ms"}
            or data.get("pair_id") != pair_id
            or type(data.get("secret_digest")) is not str
            or _DIGEST_RE.fullmatch(data["secret_digest"]) is None
            or type(data.get("burned_at_ms")) is not int
            or data["burned_at_ms"] < 0
        ):
            _fail("corrupt_state", "Mesh pairing secret burn is corrupt")
        return data

    async def burn_secret(self, pair_id: str, secret_digest: str, *, now_ms: int) -> None:
        if _DIGEST_RE.fullmatch(secret_digest) is None or type(now_ms) is not int or now_ms < 0:
            _fail("invalid_secret_burn", "invalid Mesh pairing secret burn")
        async with self._global_lock:
            existing = await self._get_secret_burn(pair_id)
            if existing is not None:
                if existing["secret_digest"] != secret_digest:
                    _fail(
                        "secret_conflict",
                        "Mesh pairing secret burn belongs to another secret",
                    )
                return
            await self._durable_put(
                self._secret_key(pair_id),
                {
                    "pair_id": pair_id,
                    "secret_digest": secret_digest,
                    "burned_at_ms": now_ms,
                },
            )

    async def is_secret_burned(
        self, pair_id: str, *, secret_digest: str | None = None
    ) -> bool:
        if secret_digest is not None and _DIGEST_RE.fullmatch(secret_digest) is None:
            _fail("invalid_secret_burn", "invalid Mesh pairing secret digest")
        data = await self._get_secret_burn(pair_id)
        if data is None:
            return False
        if secret_digest is not None and data["secret_digest"] != secret_digest:
            _fail(
                "secret_conflict",
                "Mesh pairing secret burn belongs to another secret",
            )
        return True

    # -- claim/approval nonce dedup ---------------------------------------

    def _nonce_key(self, nonce: str) -> str:
        if not (nonce.startswith("nonce_") and _KEY_TOKEN_RE.fullmatch(nonce)):
            raise MeshPairingStoreError("invalid_nonce", "invalid nonce")
        return f"{self._prefix}nonces/{nonce}.json"

    async def record_nonce(
        self,
        nonce: str,
        *,
        pair_id: str,
        claim_digest: str,
        now_ms: int,
    ) -> str:
        """Create or verify the immutable owner of a claim nonce.

        A bare "seen" bit cannot safely distinguish a crash-retry of the
        original claim from a different pairing reusing the nonce.  Bind the
        global nonce ledger to both the pair id and the exact signed claim
        digest, so only that exact claim can finish an interrupted prefix.

        Returns ``"new"``, ``"same"``, or ``"different"``.  Legacy bare
        nonce records deliberately return ``"different"``: their ownership is
        unknowable and must never authorize a recovery write.
        """

        if _PAIR_ID_RE.fullmatch(pair_id) is None:
            _fail("invalid_pair_id", "invalid pair id")
        if not isinstance(claim_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", claim_digest, re.ASCII
        ):
            _fail("invalid_claim_digest", "invalid claim digest")
        async with self._global_lock:
            key = self._nonce_key(nonce)
            existing = await self._get_json(key)
            if existing is not None:
                if set(existing) != {
                    "nonce",
                    "pair_id",
                    "claim_digest",
                    "seen_at_ms",
                }:
                    return "different"
                if (
                    existing.get("nonce") == nonce
                    and existing.get("pair_id") == pair_id
                    and existing.get("claim_digest") == claim_digest
                    and type(existing.get("seen_at_ms")) is int
                ):
                    return "same"
                return "different"
            await self._durable_put(
                key,
                {
                    "nonce": nonce,
                    "pair_id": pair_id,
                    "claim_digest": claim_digest,
                    "seen_at_ms": now_ms,
                },
            )
            return "new"

    # -- activation receipts (idempotency) --------------------------------

    def _receipt_key(self, token: str) -> str:
        if _KEY_TOKEN_RE.fullmatch(token) is None:
            raise MeshPairingStoreError("invalid_receipt", "invalid receipt token")
        return f"{self._prefix}receipts/{token}.json"

    async def has_receipt(self, token: str) -> bool:
        return (await self._get_json(self._receipt_key(token))) is not None

    async def put_receipt(self, token: str, payload: dict) -> None:
        await self._durable_put(self._receipt_key(token), payload)

    # -- durable artifact blobs (invitation/claim/approval/bootstrap) -----

    def _blob_key(self, pair_id: str, name: str) -> str:
        if _KEY_TOKEN_RE.fullmatch(name) is None:
            raise MeshPairingStoreError("invalid_blob", "invalid blob name")
        return f"{self._prefix}blobs/{self._pair_token(pair_id)}/{name}.b64"

    async def put_blob(self, pair_id: str, name: str, data: bytes) -> None:
        # Blobs (artifacts, bootstrap payload) can be large — far beyond HCJ's
        # per-string limit — so they are stored as raw base64url text with a
        # byte-identical read-back, not through the HCJ canonical writer.
        if type(data) is not bytes:
            raise MeshPairingStoreError("invalid_blob", "blob data must be bytes")
        self.assert_safe()
        encoded = base64.urlsafe_b64encode(data).decode("ascii")
        key = self._blob_key(pair_id, name)
        try:
            await self._storage.put(key, encoded)
            read = await self._storage.get(key)
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError("io_ambiguous", "Mesh pairing blob write is unsafe") from exc
        if read != encoded:
            self._mark_unsafe()
            _fail("readback_mismatch", "Mesh pairing blob failed read-back verification")

    async def get_blob(self, pair_id: str, name: str) -> Optional[bytes]:
        self.assert_safe()
        try:
            read = await self._storage.get(self._blob_key(pair_id, name))
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError("io_ambiguous", "Mesh pairing blob read is unsafe") from exc
        if read is None:
            return None
        try:
            return base64.urlsafe_b64decode(read.encode("ascii"))
        except (ValueError, TypeError) as exc:
            raise MeshPairingStoreError("corrupt_state", "Mesh pairing blob is corrupt") from exc

    # -- blocked-recovery evidence ----------------------------------------

    def _evidence_key(self, pair_id: str) -> str:
        return f"{self._prefix}evidence/{self._pair_token(pair_id)}.json"

    async def put_evidence(self, pair_id: str, signed: SignedBlockedRecoveryEvidence) -> None:
        await self._durable_put(self._evidence_key(pair_id), signed.as_dict())

    async def get_evidence(self, pair_id: str) -> Optional[SignedBlockedRecoveryEvidence]:
        data = await self._get_json(self._evidence_key(pair_id))
        if data is None:
            return None
        return SignedBlockedRecoveryEvidence.from_bytes(canonical_dumps(data))


__all__ = ["MeshPairingStore", "MeshPairingStoreError", "PairingStorage"]
