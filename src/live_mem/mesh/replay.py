# -*- coding: utf-8 -*-
"""Durable, authority-gated replay ledger for signed Mesh event delivery.

Pairing requests deliberately do not allocate replay state in P10-2.  Only an
already-authorized ``event.deliver`` request may reach :meth:`admit_verified`.
The caller proves that ordering with an exact object-identity capability chosen
when constructing the ledger; a mistaken pre-authority call fails before I/O.

The implementation supports the repository's canonical single-process runtime.
It combines per-key and global asyncio locks, writes a canonical record, reads it
back byte-for-byte, and only then reports admission.  Cancellation and storage
deadlines never release those locks while an ambiguous write remains in flight.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import re
import stat
import time
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, Protocol

from ..core.secret_fs import SecretFilesystemError, require_supported_filesystem
from .canonical import HCJError, canonical_dumps, canonical_loads
from .wire import (
    MESH_REQUEST_FRESHNESS_WINDOW_MS,
    MESH_REQUEST_SIGNATURE_DOMAIN,
    MeshHttpOperation,
    MeshRequestEnvelope,
)


REPLAY_KEY_DOMAIN: Final = b"hivemind-mesh-replay-key-v1\0"
PROCESS_LOCK_DOMAIN: Final = b"hivemind-mesh-process-lock-v1\0"
DEFAULT_GLOBAL_LIMIT: Final = 4096
DEFAULT_PER_SIGNER_LIMIT: Final = 256
# Frozen P10-2 contract: a replay nonce remains durable for the complete
# request-freshness window.  This constant lives with the persisted record
# invariant; importing the ASGI router from this storage module would create a
# circular and initialization-order-sensitive authority dependency.
TRANSPORT_REPLAY_TTL_MS: Final = MESH_REQUEST_FRESHNESS_WINDOW_MS
_MAX_SAFE_INTEGER: Final = (1 << 53) - 1

_FINGERPRINT_RE = re.compile(r"^hm1:[0-9a-f]{64}$", re.ASCII)
_NONCE_RE = re.compile(r"^nonce_[0-9a-f]{64}$", re.ASCII)
_REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$", re.ASCII)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)
_PREFIX_RE = re.compile(
    r"^_system/mesh_pairing/hm1:[0-9a-f]{64}/replay/$", re.ASCII
)
_RECORD_KEY_RE = re.compile(r"^[0-9a-f]{64}\.json$", re.ASCII)

# Permanent for the lifetime of this Python process.  Prefix scoping keeps
# independent test identities isolated; production owns one local fingerprint.
_PROCESS_UNSAFE_PREFIXES: set[str] = set()


class ReplayStorage(Protocol):
    async def put(
        self,
        key: str,
        content: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None: ...

    async def get(self, key: str) -> str | None: ...

    async def delete(self, key: str) -> None: ...

    async def list_objects(
        self, prefix: str, max_keys: int = 0
    ) -> list[dict]: ...


class ReplayError(RuntimeError):
    """Machine-readable, non-reflective replay refusal."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _require_process_lock_filesystem(fd: int) -> None:
    """Apply the reviewed local-filesystem allowlist without reflecting detail."""

    try:
        require_supported_filesystem(fd)
    except (SecretFilesystemError, OSError) as exc:
        raise ReplayError(
            "process_lock_unsafe", "Mesh process lock is unsafe"
        ) from exc


def _fail(code: str, message: str) -> "None":
    raise ReplayError(code, message)


class ReplayDecision(str, Enum):
    ADMITTED = "admitted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class ReplayRecord:
    record_version: int
    operation: str
    signer_fingerprint: str
    transport_nonce: str
    request_id: str
    space_id: str
    envelope_digest: str
    body_digest: str
    issued_at_ms: int
    admitted_at_ms: int
    expires_at_ms: int

    def __post_init__(self) -> None:
        if type(self.record_version) is not int or self.record_version != 1:
            _fail("corrupt_replay", "Replay state is invalid")
        if type(self.operation) is not str or self.operation != "event.deliver":
            _fail("corrupt_replay", "Replay state is invalid")
        checks = (
            (self.signer_fingerprint, _FINGERPRINT_RE),
            (self.transport_nonce, _NONCE_RE),
            (self.request_id, _REQUEST_ID_RE),
            (self.space_id, _SPACE_ID_RE),
            (self.envelope_digest, _DIGEST_RE),
            (self.body_digest, _DIGEST_RE),
        )
        if any(type(value) is not str or pattern.fullmatch(value) is None for value, pattern in checks):
            _fail("corrupt_replay", "Replay state is invalid")
        for value in (self.issued_at_ms, self.admitted_at_ms, self.expires_at_ms):
            if type(value) is not int or not 0 <= value <= _MAX_SAFE_INTEGER:
                _fail("corrupt_replay", "Replay state is invalid")
        if (
            self.issued_at_ms > _MAX_SAFE_INTEGER - TRANSPORT_REPLAY_TTL_MS
            or self.expires_at_ms
            != self.issued_at_ms + TRANSPORT_REPLAY_TTL_MS
            or self.admitted_at_ms > self.expires_at_ms
        ):
            _fail("corrupt_replay", "Replay state is invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "admitted_at_ms": self.admitted_at_ms,
            "body_digest": self.body_digest,
            "envelope_digest": self.envelope_digest,
            "expires_at_ms": self.expires_at_ms,
            "issued_at_ms": self.issued_at_ms,
            "operation": self.operation,
            "record_version": self.record_version,
            "request_id": self.request_id,
            "signer_fingerprint": self.signer_fingerprint,
            "space_id": self.space_id,
            "transport_nonce": self.transport_nonce,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_bytes(cls, raw: bytes) -> "ReplayRecord":
        try:
            value = canonical_loads(raw)
        except HCJError as exc:
            raise ReplayError("corrupt_replay", "Replay state is invalid") from exc
        fields = {
            "admitted_at_ms",
            "body_digest",
            "envelope_digest",
            "expires_at_ms",
            "issued_at_ms",
            "operation",
            "record_version",
            "request_id",
            "signer_fingerprint",
            "space_id",
            "transport_nonce",
        }
        if type(value) is not dict or set(value) != fields:
            _fail("corrupt_replay", "Replay state is invalid")
        return cls(**value)  # type: ignore[arg-type]


def _record_key(prefix: str, signer_fingerprint: str, nonce: str) -> str:
    material = (
        REPLAY_KEY_DOMAIN
        + MESH_REQUEST_SIGNATURE_DOMAIN
        + signer_fingerprint.encode("ascii")
        + b"\0"
        + nonce.encode("ascii")
    )
    return prefix + hashlib.sha256(material).hexdigest() + ".json"


class DurableReplayLedger:
    """Durable event replay ledger for the supported mono-process runtime."""

    def __init__(
        self,
        storage: ReplayStorage,
        *,
        prefix: str,
        authority_capability: object,
        global_limit: int = DEFAULT_GLOBAL_LIMIT,
        per_signer_limit: int = DEFAULT_PER_SIGNER_LIMIT,
        io_timeout_seconds: float = 5.0,
    ) -> None:
        if _PREFIX_RE.fullmatch(prefix) is None:
            raise ValueError("invalid Mesh replay prefix")
        if authority_capability is None:
            raise ValueError("authority capability must be a non-None object")
        if type(global_limit) is not int or not 1 <= global_limit <= DEFAULT_GLOBAL_LIMIT:
            raise ValueError("invalid replay global limit")
        if type(per_signer_limit) is not int or not 1 <= per_signer_limit <= DEFAULT_PER_SIGNER_LIMIT:
            raise ValueError("invalid replay per-signer limit")
        if per_signer_limit > global_limit:
            raise ValueError("per-signer replay limit exceeds global limit")
        if type(io_timeout_seconds) not in (int, float) or io_timeout_seconds <= 0:
            raise ValueError("invalid replay I/O timeout")
        self._storage = storage
        self._prefix = prefix
        self.__authority_capability = authority_capability
        self._global_limit = global_limit
        self._per_signer_limit = per_signer_limit
        self._io_timeout = float(io_timeout_seconds)
        self._global_lock = asyncio.Lock()
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_lock_users: dict[str, int] = {}
        self._records: dict[str, ReplayRecord] = {}
        self._loaded = False

    @property
    def unsafe(self) -> bool:
        return self._prefix in _PROCESS_UNSAFE_PREFIXES

    def assert_safe(self) -> None:
        if self.unsafe:
            _fail("local_unsafe", "Mesh replay state is unsafe")

    def _mark_unsafe(self) -> None:
        _PROCESS_UNSAFE_PREFIXES.add(self._prefix)

    async def admit_verified(
        self,
        envelope: MeshRequestEnvelope,
        *,
        authority_capability: object,
        now_ms: int,
        expires_at_ms: int,
    ) -> ReplayDecision:
        """Admit an authority-verified event or reject its durable replay.

        Capability identity is checked before validation, locks, or storage I/O.
        Pair operations are never replay-ledger inputs in P10-2.
        """

        if authority_capability is not self.__authority_capability:
            _fail("authority_required", "Replay admission requires prior authority")
        self.assert_safe()
        if type(envelope) is not MeshRequestEnvelope:
            _fail("invalid_replay", "Replay admission input is invalid")
        if envelope.op is not MeshHttpOperation.EVENTS:
            _fail("pair_replay_forbidden", "Pair operations do not use replay state")
        if type(now_ms) is not int or type(expires_at_ms) is not int:
            _fail("invalid_replay", "Replay admission timing is invalid")
        if not 0 <= now_ms <= _MAX_SAFE_INTEGER:
            _fail("invalid_replay", "Replay admission timing is invalid")
        if envelope.issued_at_ms > _MAX_SAFE_INTEGER - TRANSPORT_REPLAY_TTL_MS:
            _fail("invalid_replay", "Replay admission timing is invalid")
        expected_expiry = envelope.issued_at_ms + TRANSPORT_REPLAY_TTL_MS
        if expires_at_ms != expected_expiry:
            _fail("invalid_replay", "Replay admission expiry is invalid")
        if expires_at_ms < now_ms:
            _fail("invalid_replay", "Replay admission has already expired")

        key = _record_key(
            self._prefix, envelope.source_fingerprint, envelope.nonce
        )
        key_lock = self._key_locks.setdefault(key, asyncio.Lock())
        self._key_lock_users[key] = self._key_lock_users.get(key, 0) + 1
        try:
            async with key_lock:
                async with self._global_lock:
                    self.assert_safe()
                    await self._ensure_loaded_locked()

                    envelope_digest = hashlib.sha256(
                        envelope.canonical_bytes()
                    ).hexdigest()
                    proposed = ReplayRecord(
                        record_version=1,
                        operation=envelope.op.value,
                        signer_fingerprint=envelope.source_fingerprint,
                        transport_nonce=envelope.nonce,
                        request_id=envelope.request_id,
                        space_id=envelope.space_id,
                        envelope_digest=envelope_digest,
                        body_digest=envelope.body_digest,
                        issued_at_ms=envelope.issued_at_ms,
                        admitted_at_ms=now_ms,
                        expires_at_ms=expires_at_ms,
                    )
                    existing = self._records.get(key)
                    if existing is not None:
                        # ``admitted_at_ms`` is local receipt metadata, not part of
                        # logical replay identity.  Every signed/request field must
                        # otherwise agree exactly.
                        if self._same_logical_request(existing, proposed):
                            return ReplayDecision.DUPLICATE
                        _fail(
                            "replay_conflict",
                            "Mesh replay conflicts with durable state",
                        )

                    # The candidate nonce is compared before GC.  Otherwise a
                    # coherently altered record (for example issued-at and
                    # expiry shifted together) could look expired, be deleted,
                    # and permit the same nonce to be admitted again.  Expired
                    # records for every *other* key remain eligible for bounded
                    # garbage collection below.
                    await self._gc_expired_locked(now_ms)

                    if len(self._records) >= self._global_limit:
                        _fail(
                            "replay_saturated",
                            "Mesh replay capacity is saturated",
                        )
                    signer_count = sum(
                        record.signer_fingerprint
                        == proposed.signer_fingerprint
                        for record in self._records.values()
                    )
                    if signer_count >= self._per_signer_limit:
                        _fail(
                            "replay_saturated",
                            "Mesh replay capacity is saturated",
                        )

                    await self._commit_record_locked(key, proposed)
                    return ReplayDecision.ADMITTED
        finally:
            users = self._key_lock_users[key] - 1
            if users == 0:
                self._key_lock_users.pop(key, None)
                if self._key_locks.get(key) is key_lock:
                    self._key_locks.pop(key, None)
            else:
                self._key_lock_users[key] = users

    @staticmethod
    def _same_logical_request(left: ReplayRecord, right: ReplayRecord) -> bool:
        return (
            left.operation == right.operation
            and left.signer_fingerprint == right.signer_fingerprint
            and left.transport_nonce == right.transport_nonce
            and left.request_id == right.request_id
            and left.space_id == right.space_id
            and left.envelope_digest == right.envelope_digest
            and left.body_digest == right.body_digest
            and left.issued_at_ms == right.issued_at_ms
            and left.expires_at_ms == right.expires_at_ms
        )

    async def gc_expired(self, now_ms: int) -> int:
        if type(now_ms) is not int or not 0 <= now_ms <= _MAX_SAFE_INTEGER:
            _fail("invalid_replay", "Replay GC timing is invalid")
        self.assert_safe()
        async with self._global_lock:
            self.assert_safe()
            await self._ensure_loaded_locked()
            return await self._gc_expired_locked(now_ms)

    async def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        try:
            # One extra item distinguishes a full valid ledger from corrupt or
            # externally-written overflow without ever materializing an
            # unbounded object listing.
            objects = await self._storage.list_objects(
                self._prefix, max_keys=self._global_limit + 1
            )
            if type(objects) is not list:
                raise TypeError("invalid storage listing")
            if len(objects) > self._global_limit:
                raise ValueError("replay capacity exceeded")
            loaded: dict[str, ReplayRecord] = {}
            for item in objects:
                if type(item) is not dict or type(item.get("Key")) is not str:
                    raise ValueError("invalid storage listing")
                key = item["Key"]
                if not key.startswith(self._prefix):
                    raise ValueError("storage key escaped replay prefix")
                basename = key[len(self._prefix) :]
                if _RECORD_KEY_RE.fullmatch(basename) is None:
                    raise ValueError("unexpected replay object")
                raw_text = await self._storage.get(key)
                if type(raw_text) is not str:
                    raise ValueError("missing replay object")
                record = ReplayRecord.from_bytes(raw_text.encode("utf-8", "strict"))
                if key != _record_key(
                    self._prefix,
                    record.signer_fingerprint,
                    record.transport_nonce,
                ):
                    raise ValueError("replay key/content mismatch")
                if key in loaded:
                    raise ValueError("duplicate replay key")
                loaded[key] = record
            signer_counts = Counter(
                record.signer_fingerprint for record in loaded.values()
            )
            if any(
                count > self._per_signer_limit
                for count in signer_counts.values()
            ):
                raise ValueError("per-signer replay capacity exceeded")
            self._records = loaded
            self._loaded = True
        except BaseException as exc:
            self._mark_unsafe()
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            raise ReplayError("local_unsafe", "Mesh replay state is unsafe") from exc

    async def _gc_expired_locked(self, now_ms: int) -> int:
        # Strict inequality is normative: a record expiring exactly at ``now``
        # remains present until a later tick.
        expired = sorted(
            key
            for key, record in self._records.items()
            if record.expires_at_ms < now_ms
        )
        removed = 0
        for key in expired:
            await self._delete_record_locked(key)
            removed += 1
        return removed

    async def _commit_record_locked(self, key: str, record: ReplayRecord) -> None:
        expected = record.canonical_bytes()

        async def write_readback() -> None:
            await self._storage.put(
                key,
                expected.decode("utf-8"),
                content_type="application/json",
            )
            observed = await self._storage.get(key)
            if type(observed) is not str or observed.encode("utf-8", "strict") != expected:
                raise RuntimeError("replay write/readback mismatch")
            self._records[key] = record

        task = asyncio.create_task(write_readback())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._io_timeout)
        except asyncio.CancelledError:
            try:
                await self._settle_task(task)
            except BaseException:
                self._mark_unsafe()
            raise
        except TimeoutError:
            try:
                await self._settle_task(task)
            except BaseException as exc:
                self._mark_unsafe()
                raise ReplayError("local_unsafe", "Mesh replay state is unsafe") from exc
            # Durability is now unambiguous and the cache was updated, but the
            # caller still receives its configured deadline outcome.  A retry is
            # deterministically DUPLICATE.
            raise ReplayError(
                "storage_timeout", "Mesh replay storage deadline was exceeded"
            )
        except BaseException as exc:
            self._mark_unsafe()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ReplayError("local_unsafe", "Mesh replay state is unsafe") from exc

    async def _delete_record_locked(self, key: str) -> None:
        async def delete_readback() -> None:
            await self._storage.delete(key)
            if await self._storage.get(key) is not None:
                raise RuntimeError("replay delete/readback mismatch")
            self._records.pop(key, None)

        task = asyncio.create_task(delete_readback())
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._io_timeout)
        except asyncio.CancelledError:
            try:
                await self._settle_task(task)
            except BaseException:
                self._mark_unsafe()
            raise
        except TimeoutError:
            try:
                await self._settle_task(task)
            except BaseException as exc:
                self._mark_unsafe()
                raise ReplayError("local_unsafe", "Mesh replay state is unsafe") from exc
            raise ReplayError(
                "storage_timeout", "Mesh replay storage deadline was exceeded"
            )
        except BaseException as exc:
            self._mark_unsafe()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ReplayError("local_unsafe", "Mesh replay state is unsafe") from exc

    @staticmethod
    async def _settle_task(task: asyncio.Task[None]) -> None:
        """Wait through repeated outer cancellation until inner I/O settles."""

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()


class MeshProcessIdentityLock:
    """Retained OS lock proving the supported mono-process identity owner."""

    def __init__(self, lock_directory: str | os.PathLike[str], identity: str) -> None:
        if type(identity) is not str or _FINGERPRINT_RE.fullmatch(identity) is None:
            raise ValueError("invalid Mesh process-lock identity")
        directory = Path(lock_directory)
        if not directory.is_absolute() or not directory.name:
            raise ValueError("Mesh process-lock directory must be absolute")
        digest = hashlib.sha256(
            PROCESS_LOCK_DOMAIN + identity.encode("ascii")
        ).hexdigest()
        self._directory = directory
        self._filename = f"{digest}.lock"
        self._path = directory / self._filename
        self._content = (digest + "\n").encode("ascii")
        self._fd: int | None = None
        self._owner_pid: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None and self._owner_pid == os.getpid()

    def acquire(self, *, timeout_seconds: float = 5.0) -> None:
        if self._fd is not None:
            if self._owner_pid == os.getpid():
                return
            raise ReplayError(
                "process_lock_inherited",
                "Mesh process lock cannot be inherited across fork",
            )
        if type(timeout_seconds) not in (int, float) or timeout_seconds < 0:
            raise ValueError("invalid Mesh process-lock timeout")
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        parent_fd: int | None = None
        directory_fd: int | None = None
        try:
            parent_fd = os.open(self._directory.parent, directory_flags)
            _require_process_lock_filesystem(parent_fd)
            parent_info = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(parent_info.st_mode)
                or parent_info.st_uid != os.geteuid()
                or stat.S_IMODE(parent_info.st_mode) != 0o700
            ):
                raise ReplayError(
                    "process_lock_unsafe", "Mesh process lock is unsafe"
                )
            try:
                os.mkdir(self._directory.name, 0o700, dir_fd=parent_fd)
                os.fsync(parent_fd)
            except FileExistsError:
                pass
            directory_fd = os.open(
                self._directory.name, directory_flags, dir_fd=parent_fd
            )
            _require_process_lock_filesystem(directory_fd)
            directory_info = os.fstat(directory_fd)
            if (
                not stat.S_ISDIR(directory_info.st_mode)
                or directory_info.st_dev != parent_info.st_dev
                or directory_info.st_uid != os.geteuid()
                or stat.S_IMODE(directory_info.st_mode) != 0o700
            ):
                raise ReplayError(
                    "process_lock_unsafe", "Mesh process lock is unsafe"
                )
            flags = (
                os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            created = False
            try:
                fd = os.open(
                    self._filename,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=directory_fd,
                )
                created = True
            except FileExistsError:
                fd = os.open(self._filename, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise ReplayError("process_lock_failed", "Mesh process lock failed") from exc
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            if parent_fd is not None:
                os.close(parent_fd)
        try:
            _require_process_lock_filesystem(fd)
            if created:
                os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.geteuid()
                or info.st_dev != directory_info.st_dev
                or info.st_size not in (0, len(self._content))
            ):
                raise ReplayError(
                    "process_lock_unsafe", "Mesh process lock is unsafe"
                )
            deadline = time.monotonic() + float(timeout_seconds)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ReplayError(
                            "process_lock_timeout", "Mesh process lock is held"
                        )
                    time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
            os.lseek(fd, 0, os.SEEK_SET)
            existing = os.read(fd, len(self._content) + 1)
            if existing not in (b"", self._content):
                raise ReplayError(
                    "process_lock_unsafe", "Mesh process lock is unsafe"
                )
            if existing == b"":
                os.lseek(fd, 0, os.SEEK_SET)
                offset = 0
                while offset < len(self._content):
                    written = os.write(fd, self._content[offset:])
                    if written <= 0:
                        raise OSError("Mesh process lock write made no progress")
                    offset += written
                os.ftruncate(fd, len(self._content))
                os.fsync(fd)
            self._fd = fd
            self._owner_pid = os.getpid()
        except BaseException:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)
            raise

    def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        owner_pid = self._owner_pid
        self._owner_pid = None
        if owner_pid != os.getpid():
            # A forked child must only drop its inherited descriptor reference;
            # explicitly unlocking the shared open-file description would also
            # release the parent's protection.
            os.close(fd)
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "MeshProcessIdentityLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


__all__ = [
    "DEFAULT_GLOBAL_LIMIT",
    "DEFAULT_PER_SIGNER_LIMIT",
    "DurableReplayLedger",
    "MeshProcessIdentityLock",
    "ReplayDecision",
    "ReplayError",
    "ReplayRecord",
    "ReplayStorage",
    "TRANSPORT_REPLAY_TTL_MS",
]
