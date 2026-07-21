# -*- coding: utf-8 -*-
"""Durable local-only Project Mesh pairing store (P10-3, issue #191).

Persists pairing sessions, target reservations, burned one-time-secret markers,
claim/approval nonce dedup, activation receipts (idempotency), and signed
blocked-recovery evidence under
``_system/mesh_pairing/<instance-fingerprint>/``.

This is **local operational state** — not shared memory, not a long-memory
document, and not membership authority (PROJECT_MESH.md §4).  Every durable write
is verified by a byte-identical read-back; an ambiguous/non-identical persistence
result **poisons** the process-local store so all later operations fail closed
(mirrors the P10-2 ``DurableReplayLedger`` discipline).  An in-memory reservation
index lets the ordinary-write guard short-circuit with no durable read when the
instance holds no reservations.
"""

from __future__ import annotations

import asyncio
import base64
import re
from typing import Optional, Protocol

from .canonical import canonical_dumps, canonical_loads
from .pairing_state import (
    MeshPairingSession,
    SignedBlockedRecoveryEvidence,
)

_PREFIX_RE = re.compile(r"^_system/mesh_pairing/hm1:[0-9a-f]{64}/$", re.ASCII)
_PAIR_ID_RE = re.compile(r"^pair_[0-9a-f]{32}$", re.ASCII)
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)
_KEY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$", re.ASCII)

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
        return MeshPairingSession.from_dict(data)

    async def list_sessions(self) -> list[MeshPairingSession]:
        self.assert_safe()
        prefix = f"{self._prefix}sessions/"
        try:
            objects = await self._storage.list_objects(prefix)
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError("io_ambiguous", "Mesh pairing list is unsafe") from exc
        sessions: list[MeshPairingSession] = []
        for obj in objects:
            key = obj.get("Key") if isinstance(obj, dict) else None
            if not isinstance(key, str) or not key.endswith(".json"):
                continue
            data = await self._get_json(key)
            if data is not None:
                sessions.append(MeshPairingSession.from_dict(data))
        return sessions

    # -- reservations ------------------------------------------------------

    def _reservation_key(self, space_id: str) -> str:
        if _SPACE_ID_RE.fullmatch(space_id) is None:
            raise MeshPairingStoreError("invalid_space_id", "invalid space id")
        return f"{self._prefix}reservations/{space_id}.json"

    async def _ensure_reserved_index(self) -> dict[str, str]:
        if self._reserved is not None:
            return self._reserved
        self.assert_safe()
        prefix = f"{self._prefix}reservations/"
        try:
            objects = await self._storage.list_objects(prefix)
        except Exception as exc:
            self._mark_unsafe()
            raise MeshPairingStoreError("io_ambiguous", "Mesh reservation load is unsafe") from exc
        index: dict[str, str] = {}
        for obj in objects:
            key = obj.get("Key") if isinstance(obj, dict) else None
            if not isinstance(key, str) or not key.endswith(".json"):
                continue
            data = await self._get_json(key)
            if data is not None and isinstance(data.get("space_id"), str) and isinstance(data.get("pair_id"), str):
                index[data["space_id"]] = data["pair_id"]
        self._reserved = index
        return index

    async def reserve(self, space_id: str, pair_id: str, *, now_ms: int) -> None:
        """Reserve ``space_id`` exclusively for ``pair_id``.

        Fails closed if the space is already reserved by a DIFFERENT pairing.
        Idempotent for the same ``pair_id`` (restart-safe re-reserve).  Callers
        MUST hold :meth:`space_lock` across a virginity check and this reserve so
        a populated space is never merged (M5).
        """

        if _PAIR_ID_RE.fullmatch(pair_id) is None:
            _fail("invalid_pair_id", "invalid pair id")
        async with self._global_lock:
            index = await self._ensure_reserved_index()
            existing = index.get(space_id)
            if existing is not None and existing != pair_id:
                _fail("space_reserved", "target space is already reserved by another pairing")
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
                await self._storage.delete(self._reservation_key(space_id))
            except Exception as exc:
                self._mark_unsafe()
                raise MeshPairingStoreError("io_ambiguous", "Mesh reservation release is unsafe") from exc
            index.pop(space_id, None)

    async def get_reservation(self, space_id: str) -> Optional[str]:
        async with self._global_lock:
            index = await self._ensure_reserved_index()
            return index.get(space_id)

    async def assert_space_not_reserved(self, space_id: str) -> None:
        """Raise ``MeshPairingStoreError`` if ``space_id`` is reserved (guard hook).

        Fast path: when the in-memory index is loaded and empty, returns without a
        durable read.  Fail-closed is scoped to the reserved space only; a store
        that is process-poisoned refuses (unsafe), but that never silently admits
        a write to a reserved target.
        """

        # Fast negative short-circuit without taking the global lock when the
        # index is already hydrated and holds no reservations for this space.
        cached = self._reserved
        if cached is not None and space_id not in cached:
            return
        reserved_by = await self.get_reservation(space_id)
        if reserved_by is not None:
            _fail("space_reserved", "space is reserved for a Project Mesh pairing")

    # -- one-time secret burn ---------------------------------------------

    def _secret_key(self, pair_id: str) -> str:
        return f"{self._prefix}secrets/{self._pair_token(pair_id)}.json"

    def _pair_token(self, pair_id: str) -> str:
        if _PAIR_ID_RE.fullmatch(pair_id) is None:
            raise MeshPairingStoreError("invalid_pair_id", "invalid pair id")
        return pair_id

    async def burn_secret(self, pair_id: str, secret_digest: str, *, now_ms: int) -> None:
        await self._durable_put(
            self._secret_key(pair_id),
            {"pair_id": pair_id, "secret_digest": secret_digest, "burned_at_ms": now_ms},
        )

    async def is_secret_burned(self, pair_id: str) -> bool:
        return (await self._get_json(self._secret_key(pair_id))) is not None

    # -- claim/approval nonce dedup ---------------------------------------

    def _nonce_key(self, nonce: str) -> str:
        if not (nonce.startswith("nonce_") and _KEY_TOKEN_RE.fullmatch(nonce)):
            raise MeshPairingStoreError("invalid_nonce", "invalid nonce")
        return f"{self._prefix}nonces/{nonce}.json"

    async def record_nonce(self, nonce: str, *, now_ms: int) -> bool:
        """Record ``nonce`` once. Return True if new, False if already present."""

        async with self._global_lock:
            key = self._nonce_key(nonce)
            if (await self._get_json(key)) is not None:
                return False
            await self._durable_put(key, {"nonce": nonce, "seen_at_ms": now_ms})
            return True

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
