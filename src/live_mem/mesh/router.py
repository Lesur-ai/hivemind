# -*- coding: utf-8 -*-
"""Bounded ASGI route skeletons for the default-on Project Mesh peer namespace.

P10-2 authenticates and authorizes the transport boundary but implements no
pairing or event business mutation.  Pair routes never touch storage or replay.
An otherwise-admissible event consumes durable transport replay and then ends
with a signed ``OPERATION_UNAVAILABLE`` response.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

from pydantic import ValidationError

from ..core.hivemind.models import (
    EventEnvelope,
    HiveNodeStatus,
    Member,
    MemberStatus,
    MembershipView,
)
from ..core.hivemind.peer import required_scope_for_event
from ..core.hivemind.state import HivemindStateStore
from .canonical import HCJError, HCJLimits, canonical_dumps, canonical_loads
from .config import MeshEnabledConfig
from .identity import (
    MeshIdentityError,
    decode_membership_public_key,
    decode_mesh_public_key,
)
from .wire import (
    MESH_REQUEST_FRESHNESS_WINDOW_MS,
    MESH_REQUEST_SIGNATURE_DOMAIN,
    MESH_RESPONSE_STATUS,
    MESH_ROUTES,
    MeshHttpOperation,
    MeshRequestEnvelope,
    MeshResponseCode,
    MeshResponseEnvelope,
    MeshRoute,
    MeshWireError,
    mesh_headers,
    parse_mesh_headers,
)


logger = logging.getLogger("live_mem.mesh.router")

MAX_REQUEST_HEADERS: Final = 64
MAX_REQUEST_HEADER_BYTES: Final = 16_384
MAX_HEADER_NAME_BYTES: Final = 64
MAX_HEADER_VALUE_BYTES: Final = 4_096
FRESHNESS_WINDOW_MS: Final = MESH_REQUEST_FRESHNESS_WINDOW_MS

_PAIR_PATH_PREFIX: Final = "/mesh/v1/pair/"
_PAIR_PATH_SUFFIXES: Final = ("/status", "/bootstrap", "/ack")
_CONTENT_TYPE: Final = b"application/json"


class _StorageFactory(Protocol):
    def __call__(self) -> Any: ...


class _ProcessLock(Protocol):
    @property
    def acquired(self) -> bool: ...


ClockMilliseconds = Callable[[], int]
NonceFactory = Callable[[], str]


class _EdgeRefusal(Exception):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__("mesh_request_rejected")


class _LocalUnsafe(Exception):
    pass


class _SourceUnauthorized(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _AuthorizedEvent:
    event: EventEnvelope
    source_member: Member
    membership: MembershipView


def _default_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _default_nonce() -> str:
    return f"nonce_{secrets.token_hex(32)}"


def _default_storage_factory() -> Any:
    from ..core.storage import get_storage

    return get_storage()


def _edge_body() -> bytes:
    return canonical_dumps({"error": "MESH_REQUEST_REJECTED"})


def _protocol_body(code: MeshResponseCode) -> bytes:
    return canonical_dumps({"acknowledged": False, "code": code.value})


class MeshNamespaceRouter:
    """Reserve and terminate the complete enabled `/mesh/v1*` namespace."""

    def __init__(
        self,
        fallback: Any,
        *,
        config: MeshEnabledConfig,
        process_lock: _ProcessLock,
        storage_factory: _StorageFactory = _default_storage_factory,
        clock_ms: ClockMilliseconds = _default_clock_ms,
        nonce_factory: NonceFactory = _default_nonce,
        replay_ledger: Any | None = None,
        pairing_service: Any | None = None,
    ) -> None:
        if not isinstance(config, MeshEnabledConfig) or config.enabled is not True:
            raise ValueError("MeshNamespaceRouter requires enabled configuration")
        if getattr(process_lock, "acquired", False) is not True:
            raise ValueError("MeshNamespaceRouter requires an acquired process lock")
        self._fallback = fallback
        self._config = config
        # P10-3 durable pairing orchestration. ``None`` preserves the P10-2
        # authenticated-skeleton behaviour (routes answer OPERATION_UNAVAILABLE).
        self._pairing_service = pairing_service
        # Retain the lock object for the entire router/process lifetime.  It is
        # intentionally not released by a request or replay-ledger lifecycle.
        self._process_lock = process_lock
        self._storage_factory = storage_factory
        self._clock_ms = clock_ms
        self._nonce_factory = nonce_factory
        # Unforgeable-by-value process capability: the replay ledger checks
        # object identity both at construction and admission before any I/O.
        self._replay_authority = object()
        self._replay_ledger = replay_ledger

    @staticmethod
    def is_mesh_namespace(scope: dict[str, Any]) -> bool:
        """Return whether an HTTP scope belongs to the broadly reserved prefix."""

        if scope.get("type") != "http":
            return False
        raw_path = scope.get("raw_path")
        if isinstance(raw_path, bytes) and raw_path.startswith(b"/mesh/v1"):
            return True
        path = scope.get("path")
        return isinstance(path, str) and path.startswith("/mesh/v1")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if not self.is_mesh_namespace(scope):
            await self._fallback(scope, receive, send)
            return
        # A pre-fork application object would inherit the lock file descriptor
        # into multiple workers.  The lock records its acquisition PID, so each
        # Mesh request rechecks ownership and fails before parsing or storage.
        if self._process_lock.acquired is not True:
            await self._send_unsigned(send, 503)
            return
        try:
            await self._handle(scope, receive, send)
        except _EdgeRefusal as refusal:
            await self._send_unsigned(send, refusal.status)
        except Exception:
            # Never serialize or log exception text: lower layers may carry an
            # endpoint, corrupt stored bytes, or another sensitive value.
            logger.error("Project Mesh request failed closed")
            await self._send_unsigned(send, 503)

    async def _handle(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        headers = self._validate_request_headers(scope.get("headers", ()))
        method, raw_path, route = self._select_route(scope)
        body = await self._read_body(
            receive,
            maximum=self._config.control_max_bytes,
            declared_length=self._validate_framing(headers, method=method),
        )

        # Header syntax/cardinality is an unsigned 400. Once decoded, every
        # envelope/key/signature/freshness/target/binding failure is the same
        # unsigned 401 so the edge is not an identity oracle.
        try:
            envelope_bytes, signature = parse_mesh_headers(
                tuple(scope.get("headers", ()))
            )
        except MeshWireError:
            raise _EdgeRefusal(400) from None
        try:
            envelope = MeshRequestEnvelope.from_bytes(envelope_bytes)
            envelope.bind_request(method=method, path=raw_path, body=body)
            if envelope.op is not route.operation:
                raise MeshWireError("authentication_failed", "Mesh authentication failed")
            if envelope.target_fingerprint != self._config.fingerprint:
                raise MeshWireError("authentication_failed", "Mesh authentication failed")
            now_ms = self._safe_now_ms()
            if abs(now_ms - envelope.issued_at_ms) > FRESHNESS_WINDOW_MS:
                raise MeshWireError("authentication_failed", "Mesh authentication failed")
            envelope.verify(signature)
        except (MeshWireError, TypeError, ValueError):
            raise _EdgeRefusal(401) from None

        if envelope.op is not MeshHttpOperation.EVENTS:
            # P10-3 durable pair routes: delegate to the pairing service. Without
            # a service configured this preserves the P10-2 authenticated-skeleton
            # behaviour (no storage/replay/session/membership/bootstrap access).
            if self._pairing_service is None:
                await self._send_signed(
                    send, request=envelope, code=MeshResponseCode.OPERATION_UNAVAILABLE
                )
                return
            try:
                result = await self._pairing_service.handle_pair_request(envelope, body)
            except Exception:
                await self._send_signed(
                    send, request=envelope, code=MeshResponseCode.OPERATION_UNAVAILABLE
                )
                return
            await self._send_signed_body(send, request=envelope, code=result.code, body=result.body)
            return

        try:
            event = self._parse_event(body, envelope)
        except (HCJError, ValidationError, ValueError, TypeError):
            await self._send_signed(
                send,
                request=envelope,
                code=MeshResponseCode.INVALID_EVENT,
            )
            return

        # A process-poisoned ledger is local critical state.  Observe only its
        # in-memory capability bit here (no storage/list/write): it must mask
        # source and epoch oracles, while malformed event bodies still retain
        # their earlier signed INVALID_EVENT refusal.
        if self._replay_capability_is_unsafe():
            await self._send_signed(
                send,
                request=envelope,
                code=MeshResponseCode.LOCAL_UNSAFE,
            )
            return

        try:
            authorized = await self._authorize_event(envelope, event)
        except _LocalUnsafe:
            # Confined P10-3 pending-self-activation: the ONLY case in which a
            # would-be LOCAL_UNSAFE becomes a success, and ONLY for the exact
            # session-bound event that promotes THIS pending node e+1 -> e+2. The
            # frozen refusal ordering above is unchanged, so every other event
            # (malformed, wrong-op, poisoned-ledger, ordinary peer event) still
            # receives the byte-identical LOCAL_UNSAFE re-emitted here on any
            # predicate mismatch. See membership_sync / pairing_service.
            if self._pairing_service is not None:
                try:
                    activation = await self._pairing_service.try_pending_self_activation(
                        envelope, event
                    )
                except Exception:
                    activation = None
                if activation is not None:
                    await self._send_signed_body(
                        send, request=envelope, code=activation.code, body=activation.body
                    )
                    return
            await self._send_signed(
                send,
                request=envelope,
                code=MeshResponseCode.LOCAL_UNSAFE,
            )
            return
        except _SourceUnauthorized:
            await self._send_signed(
                send,
                request=envelope,
                code=MeshResponseCode.SOURCE_NOT_AUTHORIZED,
            )
            return

        if envelope.membership_epoch != authorized.event.membership_epoch:
            # Defensive: _parse_event already binds the two exact values.
            await self._send_signed(
                send,
                request=envelope,
                code=MeshResponseCode.INVALID_EVENT,
            )
            return

        # Source authorization deliberately precedes the epoch oracle, using
        # the same immutable view read so no second-read TOCTOU can authorize
        # against one roster and compare the epoch against another.
        if envelope.membership_epoch != authorized.membership.epoch:
            await self._send_signed(
                send,
                request=envelope,
                code=MeshResponseCode.EPOCH_MISMATCH,
            )
            return

        replay_code = await self._admit_replay(envelope, now_ms=now_ms)
        if replay_code is not None:
            await self._send_signed(send, request=envelope, code=replay_code)
            return

        # P10-3: apply the authorized, epoch-matched, replay-admitted shared event
        # (post-pairing full-mesh sync). Without a pairing service this preserves
        # the P10-2 authenticated-skeleton OPERATION_UNAVAILABLE.
        if self._pairing_service is None:
            await self._send_signed(
                send, request=envelope, code=MeshResponseCode.OPERATION_UNAVAILABLE
            )
            return
        try:
            result = await self._pairing_service.handle_event(envelope, authorized.event)
        except Exception:
            await self._send_signed(
                send, request=envelope, code=MeshResponseCode.OPERATION_UNAVAILABLE
            )
            return
        await self._send_signed_body(send, request=envelope, code=result.code, body=result.body)

    def _replay_capability_is_unsafe(self) -> bool:
        ledger = self._replay_ledger
        if ledger is None:
            return False
        checker = getattr(ledger, "assert_safe", None)
        if checker is None:
            return False
        try:
            checker()
        except Exception:
            return True
        return False

    def _validate_request_headers(
        self, raw_headers: object
    ) -> dict[bytes, bytes]:
        if not isinstance(raw_headers, Sequence) or isinstance(
            raw_headers, (bytes, str)
        ):
            raise _EdgeRefusal(400)
        if len(raw_headers) > MAX_REQUEST_HEADERS:
            raise _EdgeRefusal(431)
        by_name: dict[bytes, bytes] = {}
        wire_bytes = 0
        for item in raw_headers:
            if type(item) is not tuple or len(item) != 2:
                raise _EdgeRefusal(400)
            name, value = item
            if type(name) is not bytes or type(value) is not bytes:
                raise _EdgeRefusal(400)
            wire_bytes += len(name) + 2 + len(value) + 2
            if wire_bytes > MAX_REQUEST_HEADER_BYTES:
                raise _EdgeRefusal(431)
            if (
                not name
                or len(name) > MAX_HEADER_NAME_BYTES
                or len(value) > MAX_HEADER_VALUE_BYTES
                or any(byte > 0x7F for byte in name + value)
                or any(byte in value for byte in b"\r\n\x00")
                or not all(
                    chr(byte).isalnum() or byte in b"!#$%&'*+-.^_`|~"
                    for byte in name
                )
            ):
                raise _EdgeRefusal(400)
            lowered = name.lower()
            if lowered in by_name:
                raise _EdgeRefusal(400)
            by_name[lowered] = value
        return by_name

    def _select_route(
        self, scope: dict[str, Any]
    ) -> tuple[str, str, MeshRoute]:
        method = scope.get("method")
        path = scope.get("path")
        raw_path = scope.get("raw_path")
        query = scope.get("query_string", b"")
        if (
            type(method) is not str
            or not method.isascii()
            or type(path) is not str
            or not path.isascii()
            or type(query) is not bytes
            or query
        ):
            raise _EdgeRefusal(400)
        if raw_path is None:
            raw_path = path.encode("ascii")
        if type(raw_path) is not bytes:
            raise _EdgeRefusal(400)
        try:
            exact_raw_path = raw_path.decode("ascii", "strict")
        except UnicodeDecodeError:
            raise _EdgeRefusal(400) from None
        if exact_raw_path != path or any(char in exact_raw_path for char in "%\\?#"):
            raise _EdgeRefusal(400)

        path_matches = [
            route
            for route in MESH_ROUTES.values()
            if route.match_pair_id(exact_raw_path) is not None
        ]
        if not path_matches:
            if self._looks_like_malformed_pair_path(exact_raw_path):
                raise _EdgeRefusal(400)
            raise _EdgeRefusal(404)
        route = path_matches[0]
        if method != route.method:
            raise _EdgeRefusal(405)
        return method, exact_raw_path, route

    @staticmethod
    def _looks_like_malformed_pair_path(path: str) -> bool:
        return path.startswith(_PAIR_PATH_PREFIX) and any(
            path.endswith(suffix) for suffix in _PAIR_PATH_SUFFIXES
        )

    def _validate_framing(self, headers: dict[bytes, bytes], *, method: str) -> int | None:
        if b"content-encoding" in headers or b"trailer" in headers:
            raise _EdgeRefusal(400)
        content_length: int | None = None
        if b"content-length" in headers:
            raw = headers[b"content-length"]
            if (
                not raw
                or len(raw) > 20
                or not raw.isdigit()
                or (len(raw) > 1 and raw.startswith(b"0"))
            ):
                raise _EdgeRefusal(400)
            content_length = int(raw)
            if content_length > self._config.control_max_bytes:
                raise _EdgeRefusal(413)
        transfer_encoding = headers.get(b"transfer-encoding")
        if transfer_encoding is not None:
            if transfer_encoding.lower() != b"chunked" or content_length is not None:
                raise _EdgeRefusal(400)
        if method == "POST":
            if headers.get(b"content-type", b"").lower() != _CONTENT_TYPE:
                raise _EdgeRefusal(400)
        else:
            if content_length not in (None, 0) or transfer_encoding is not None:
                raise _EdgeRefusal(400)
        return content_length

    @staticmethod
    async def _read_body(
        receive: Any,
        *,
        maximum: int,
        declared_length: int | None,
    ) -> bytes:
        body = bytearray()
        while True:
            message = await receive()
            if not isinstance(message, dict) or message.get("type") != "http.request":
                raise _EdgeRefusal(400)
            chunk = message.get("body", b"")
            if type(chunk) is not bytes:
                raise _EdgeRefusal(400)
            if len(body) + len(chunk) > maximum:
                raise _EdgeRefusal(413)
            body.extend(chunk)
            more = message.get("more_body", False)
            if type(more) is not bool:
                raise _EdgeRefusal(400)
            if not more:
                break
        if declared_length is not None and declared_length != len(body):
            raise _EdgeRefusal(400)
        return bytes(body)

    def _parse_event(
        self, body: bytes, envelope: MeshRequestEnvelope
    ) -> EventEnvelope:
        limits = HCJLimits(max_total_bytes=self._config.control_max_bytes)
        decoded = canonical_loads(body, limits=limits)
        if type(decoded) is not dict:
            raise ValueError("event body must be an object")
        event = EventEnvelope.model_validate(decoded)
        normalized = canonical_dumps(event.model_dump(mode="json"), limits=limits)
        if normalized != body:
            raise ValueError("event body must be exact and non-coercive")
        if (
            event.protocol_version != 1
            or event.request_id != envelope.request_id
            or event.membership_epoch != envelope.membership_epoch
        ):
            raise ValueError("event binding is invalid")
        return event

    async def _authorize_event(
        self,
        envelope: MeshRequestEnvelope,
        event: EventEnvelope,
    ) -> _AuthorizedEvent:
        try:
            storage = self._storage_factory()
            store = HivemindStateStore(storage, envelope.space_id)
            node = await store.get_node_identity()
            membership = await store.get_membership()
            health = await store.get_node_status()
        except Exception:
            raise _LocalUnsafe from None
        if (
            node is None
            or membership is None
            or health is None
            or node.protocol_version != 1
            or membership.protocol_version != 1
            or health.protocol_version != 1
            or health.status != HiveNodeStatus.HEALTHY.value
        ):
            raise _LocalUnsafe

        active = [
            member
            for member in membership.members
            if member.status == MemberStatus.ACTIVE.value
        ]
        try:
            configured_raw = decode_mesh_public_key(self._config.public_key)
            node_raw = decode_membership_public_key(node.public_key)
            active_raw = [decode_membership_public_key(member.public_key) for member in active]
        except (MeshIdentityError, TypeError, ValueError):
            raise _LocalUnsafe from None
        node_ids = [member.node_id for member in active]
        if (
            not active
            or any(not node_id for node_id in node_ids)
            or len(set(node_ids)) != len(node_ids)
            or len(set(active_raw)) != len(active_raw)
            or node_raw != configured_raw
        ):
            raise _LocalUnsafe
        local_members = [
            member
            for member, raw in zip(active, active_raw, strict=True)
            if raw == configured_raw
        ]
        if len(local_members) != 1 or local_members[0].node_id != node.node_id:
            raise _LocalUnsafe

        try:
            source_raw = decode_mesh_public_key(envelope.source_public_key)
        except (MeshIdentityError, TypeError, ValueError):
            raise _SourceUnauthorized from None
        if source_raw == configured_raw:
            raise _SourceUnauthorized
        source_members = [
            member
            for member, raw in zip(active, active_raw, strict=True)
            if raw == source_raw
        ]
        if len(source_members) != 1:
            raise _SourceUnauthorized
        source_member = source_members[0]
        required = required_scope_for_event(event.type)
        if (
            source_member.node_id != event.origin_node_id
            or not source_member.has_scope(required)
        ):
            raise _SourceUnauthorized
        return _AuthorizedEvent(
            event=event,
            source_member=source_member,
            membership=membership,
        )

    async def _admit_replay(
        self,
        envelope: MeshRequestEnvelope,
        *,
        now_ms: int,
    ) -> MeshResponseCode | None:
        try:
            if self._replay_ledger is None:
                from .replay import DurableReplayLedger

                self._replay_ledger = DurableReplayLedger(
                    self._storage_factory(),
                    prefix=(
                        "_system/mesh_pairing/"
                        f"{self._config.fingerprint}/replay/"
                    ),
                    authority_capability=self._replay_authority,
                )
            from .replay import ReplayDecision, ReplayError

            decision = await self._replay_ledger.admit_verified(
                envelope,
                authority_capability=self._replay_authority,
                now_ms=now_ms,
                expires_at_ms=envelope.issued_at_ms + FRESHNESS_WINDOW_MS,
            )
            if decision is ReplayDecision.ADMITTED:
                return None
            if decision is ReplayDecision.DUPLICATE:
                return MeshResponseCode.REPLAY_REJECTED
            return MeshResponseCode.LOCAL_UNSAFE
        except ImportError:
            return MeshResponseCode.LOCAL_UNSAFE
        except ReplayError as exc:
            if exc.code in {"replay_conflict"}:
                return MeshResponseCode.REPLAY_REJECTED
            return MeshResponseCode.LOCAL_UNSAFE
        except Exception:
            return MeshResponseCode.LOCAL_UNSAFE

    def _safe_now_ms(self) -> int:
        value = self._clock_ms()
        if type(value) is not int or not 0 <= value <= ((1 << 53) - 1):
            raise MeshWireError("authentication_failed", "Mesh authentication failed")
        return value

    async def _send_unsigned(self, send: Any, status: int) -> None:
        body = _edge_body()
        headers = [
            (b"content-type", _CONTENT_TYPE),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _send_signed(
        self,
        send: Any,
        *,
        request: MeshRequestEnvelope,
        code: MeshResponseCode,
    ) -> None:
        body = _protocol_body(code)
        envelope = MeshResponseEnvelope.create(
            code=code,
            correlation_id=request.request_id,
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            target_fingerprint=request.source_fingerprint,
            issued_at_ms=self._safe_now_ms(),
            nonce=self._nonce_factory(),
            body=body,
        )
        signature = envelope.sign(self._config.private_key)
        headers = [
            (b"content-type", _CONTENT_TYPE),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            *mesh_headers(envelope, signature),
        ]
        await send(
            {
                "type": "http.response.start",
                "status": MESH_RESPONSE_STATUS[code],
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _send_signed_body(
        self,
        send: Any,
        *,
        request: MeshRequestEnvelope,
        code: MeshResponseCode,
        body: bytes,
    ) -> None:
        """Sign and emit a response whose body is the P10-3 handler's exact bytes.

        Unlike :meth:`_send_signed` (fixed protocol-code body), this carries a
        real data/ACK body whose digest the response envelope binds.
        """

        envelope = MeshResponseEnvelope.create(
            code=code,
            correlation_id=request.request_id,
            source_public_key=self._config.public_key,
            source_fingerprint=self._config.fingerprint,
            target_fingerprint=request.source_fingerprint,
            issued_at_ms=self._safe_now_ms(),
            nonce=self._nonce_factory(),
            body=body,
        )
        signature = envelope.sign(self._config.private_key)
        headers = [
            (b"content-type", _CONTENT_TYPE),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
            *mesh_headers(envelope, signature),
        ]
        await send(
            {
                "type": "http.response.start",
                "status": MESH_RESPONSE_STATUS[code],
                "headers": headers,
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = [
    "FRESHNESS_WINDOW_MS",
    "MAX_REQUEST_HEADER_BYTES",
    "MAX_REQUEST_HEADERS",
    "MeshNamespaceRouter",
]
