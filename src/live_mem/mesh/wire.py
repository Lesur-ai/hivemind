# -*- coding: utf-8 -*-
"""Strict signed HTTP wire envelopes for Project Mesh V1.

The envelope and signature travel in two singleton HTTP headers.  The envelope
is HCJ-1 encoded and then unpadded base64url encoded.  This module validates only
wire facts; membership, health, replay authority, and business behavior remain
outside it.
"""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping, Sequence

from cryptography.exceptions import InvalidSignature

from .canonical import HCJError, HCJLimits, canonical_dumps, canonical_loads
from .identity import (
    MeshIdentityError,
    MeshPrivateKey,
    mesh_identity_fingerprint,
    parse_mesh_public_key,
)


MESH_PROTOCOL_VERSION: Final = 1
MESH_REQUEST_FRESHNESS_WINDOW_MS: Final = 300_000
MESH_REQUEST_SIGNATURE_DOMAIN: Final = b"hivemind-mesh-request-v1\0"
MESH_RESPONSE_SIGNATURE_DOMAIN: Final = b"hivemind-mesh-response-v1\0"

MESH_ENVELOPE_HEADER: Final = b"hivemind-mesh-envelope"
MESH_SIGNATURE_HEADER: Final = b"hivemind-mesh-signature"
MESH_ENVELOPE_HEADER_DISPLAY: Final = "Hivemind-Mesh-Envelope"
MESH_SIGNATURE_HEADER_DISPLAY: Final = "Hivemind-Mesh-Signature"

MAX_ENVELOPE_HEADER_BYTES: Final = 4096
MAX_ENVELOPE_DECODED_BYTES: Final = 3072
MAX_SIGNATURE_HEADER_BYTES: Final = 86  # unpadded base64url of 64 bytes

MESH_TARGET_UNBOUND: Final = "mesh-target-unbound-v1"

_PAIR_ID_RE = re.compile(r"^pair_[0-9a-f]{32}$", re.ASCII)
_REQUEST_ID_RE = re.compile(r"^(?:pair_|req_)[0-9a-f]{32}$", re.ASCII)
_EVENT_REQUEST_ID_RE = re.compile(r"^req_[0-9a-f]{32}$", re.ASCII)
_NONCE_RE = re.compile(r"^nonce_[0-9a-f]{64}$", re.ASCII)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_FINGERPRINT_RE = re.compile(r"^hm1:[0-9a-f]{64}$", re.ASCII)
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)
_B64URL_RE = re.compile(rb"^[A-Za-z0-9_-]+$")

_ENVELOPE_HCJ_LIMITS = HCJLimits(max_total_bytes=MAX_ENVELOPE_DECODED_BYTES)


class MeshWireError(ValueError):
    """Machine-readable, non-reflective wire refusal."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _fail(code: str, message: str) -> "None":
    raise MeshWireError(code, message)


class MeshHttpOperation(str, Enum):
    """The complete and closed peer HTTP operation set."""

    PAIR_CLAIM = "pair.claim"
    PAIR_STATUS = "pair.status"
    PAIR_BOOTSTRAP = "pair.bootstrap"
    PAIR_ACK = "pair.ack"
    EVENTS = "event.deliver"


@dataclass(frozen=True, slots=True)
class MeshRoute:
    operation: MeshHttpOperation
    method: str
    path_template: str
    pair_id_in_path: bool

    def path_for(self, pair_id: str | None = None) -> str:
        if self.pair_id_in_path:
            if type(pair_id) is not str or _PAIR_ID_RE.fullmatch(pair_id) is None:
                _fail("invalid_pair_id", "Mesh pair id is invalid")
            return self.path_template.replace("{pair_id}", pair_id)
        if pair_id is not None:
            _fail("unexpected_pair_id", "This Mesh route has no pair id in its path")
        return self.path_template

    def match_pair_id(self, path: str) -> str | None:
        if type(path) is not str or not path.isascii():
            return None
        if not self.pair_id_in_path:
            return "" if path == self.path_template else None
        prefix, suffix = self.path_template.split("{pair_id}", 1)
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        end = len(path) - len(suffix) if suffix else len(path)
        pair_id = path[len(prefix) : end]
        return pair_id if _PAIR_ID_RE.fullmatch(pair_id) else None


MESH_ROUTES: Mapping[MeshHttpOperation, MeshRoute] = MappingProxyType(
    {
        MeshHttpOperation.PAIR_CLAIM: MeshRoute(
            MeshHttpOperation.PAIR_CLAIM,
            "POST",
            "/mesh/v1/pair/claim",
            False,
        ),
        MeshHttpOperation.PAIR_STATUS: MeshRoute(
            MeshHttpOperation.PAIR_STATUS,
            "GET",
            "/mesh/v1/pair/{pair_id}/status",
            True,
        ),
        MeshHttpOperation.PAIR_BOOTSTRAP: MeshRoute(
            MeshHttpOperation.PAIR_BOOTSTRAP,
            "GET",
            "/mesh/v1/pair/{pair_id}/bootstrap",
            True,
        ),
        MeshHttpOperation.PAIR_ACK: MeshRoute(
            MeshHttpOperation.PAIR_ACK,
            "POST",
            "/mesh/v1/pair/{pair_id}/ack",
            True,
        ),
        MeshHttpOperation.EVENTS: MeshRoute(
            MeshHttpOperation.EVENTS,
            "POST",
            "/mesh/v1/events",
            False,
        ),
    }
)


def _require_plain_str(value: object, *, code: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        _fail(code, "Mesh envelope field is invalid")
    return value


def _require_nonnegative_safe_int(value: object, *, code: str) -> int:
    if type(value) is not int or value < 0 or value > ((1 << 53) - 1):
        _fail(code, "Mesh envelope integer is invalid")
    return value


def body_sha256(body: bytes) -> str:
    if type(body) is not bytes:
        _fail("invalid_body_type", "Mesh body must be plain bytes")
    return hashlib.sha256(body).hexdigest()


def _validate_source_identity(public_key: object, fingerprint: object) -> tuple[str, str]:
    if type(public_key) is not str or type(fingerprint) is not str:
        _fail("invalid_source_identity", "Mesh source identity is invalid")
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        _fail("invalid_source_identity", "Mesh source identity is invalid")
    try:
        computed = mesh_identity_fingerprint(public_key)
    except (MeshIdentityError, TypeError, ValueError) as exc:
        raise MeshWireError(
            "invalid_source_identity", "Mesh source identity is invalid"
        ) from exc
    if computed != fingerprint:
        _fail("source_identity_mismatch", "Mesh source identity is inconsistent")
    return public_key, fingerprint


_REQUEST_FIELDS = frozenset(
    {
        "body_digest",
        "issued_at_ms",
        "membership_epoch",
        "method",
        "nonce",
        "op",
        "path",
        "protocol_version",
        "request_id",
        "source_fingerprint",
        "source_public_key",
        "space_id",
        "target_fingerprint",
    }
)


@dataclass(frozen=True, slots=True)
class MeshRequestEnvelope:
    protocol_version: int
    op: MeshHttpOperation
    method: str
    path: str
    space_id: str
    source_public_key: str
    source_fingerprint: str
    target_fingerprint: str
    membership_epoch: int
    request_id: str
    nonce: str
    issued_at_ms: int
    body_digest: str

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            _fail("wrong_protocol_version", "Mesh protocol version is incompatible")
        if not isinstance(self.op, MeshHttpOperation):
            _fail("invalid_operation", "Mesh HTTP operation is invalid")
        if type(self.method) is not str or not self.method.isascii():
            _fail("invalid_method", "Mesh HTTP method is invalid")
        if type(self.path) is not str or not self.path.isascii():
            _fail("invalid_path", "Mesh HTTP path is invalid")
        _require_plain_str(self.space_id, code="invalid_space_id", pattern=_SPACE_ID_RE)
        _validate_source_identity(self.source_public_key, self.source_fingerprint)
        _require_plain_str(
            self.target_fingerprint,
            code="invalid_target_identity",
            pattern=_FINGERPRINT_RE,
        )
        if self.target_fingerprint == MESH_TARGET_UNBOUND:
            _fail("unbound_target_forbidden", "Mesh HTTP requests require a bound target")
        _require_nonnegative_safe_int(
            self.membership_epoch, code="invalid_membership_epoch"
        )
        _require_plain_str(self.request_id, code="invalid_request_id", pattern=_REQUEST_ID_RE)
        _require_plain_str(self.nonce, code="invalid_nonce", pattern=_NONCE_RE)
        _require_nonnegative_safe_int(self.issued_at_ms, code="invalid_issued_at")
        _require_plain_str(self.body_digest, code="invalid_body_digest", pattern=_DIGEST_RE)

        route = MESH_ROUTES[self.op]
        if self.method != route.method:
            _fail("operation_method_mismatch", "Mesh operation and method do not match")
        path_pair_id = route.match_pair_id(self.path)
        if path_pair_id is None:
            _fail("operation_path_mismatch", "Mesh operation and path do not match")
        if self.op is MeshHttpOperation.EVENTS:
            if _EVENT_REQUEST_ID_RE.fullmatch(self.request_id) is None:
                _fail("invalid_request_id", "Mesh event request id is invalid")
        else:
            if _PAIR_ID_RE.fullmatch(self.request_id) is None:
                _fail("invalid_request_id", "Mesh pair request id is invalid")
            if route.pair_id_in_path and path_pair_id != self.request_id:
                _fail("pair_id_mismatch", "Mesh path and request pair id do not match")
        if route.method == "GET" and self.body_digest != body_sha256(b""):
            _fail("get_body_forbidden", "Mesh GET requests must have an empty body")

    @classmethod
    def create(
        cls,
        *,
        op: MeshHttpOperation,
        path: str,
        space_id: str,
        source_public_key: str,
        source_fingerprint: str,
        target_fingerprint: str,
        membership_epoch: int,
        request_id: str,
        nonce: str,
        issued_at_ms: int,
        body: bytes,
    ) -> "MeshRequestEnvelope":
        if not isinstance(op, MeshHttpOperation):
            _fail("invalid_operation", "Mesh HTTP operation is invalid")
        return cls(
            protocol_version=MESH_PROTOCOL_VERSION,
            op=op,
            method=MESH_ROUTES[op].method,
            path=path,
            space_id=space_id,
            source_public_key=source_public_key,
            source_fingerprint=source_fingerprint,
            target_fingerprint=target_fingerprint,
            membership_epoch=membership_epoch,
            request_id=request_id,
            nonce=nonce,
            issued_at_ms=issued_at_ms,
            body_digest=body_sha256(body),
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MeshRequestEnvelope":
        try:
            value = canonical_loads(raw, limits=_ENVELOPE_HCJ_LIMITS)
        except HCJError as exc:
            raise MeshWireError("invalid_envelope", "Mesh envelope is invalid") from exc
        if type(value) is not dict or frozenset(value) != _REQUEST_FIELDS:
            _fail("invalid_envelope_shape", "Mesh request envelope shape is invalid")
        try:
            operation = MeshHttpOperation(value["op"])
        except (ValueError, TypeError) as exc:
            raise MeshWireError(
                "invalid_operation", "Mesh HTTP operation is invalid"
            ) from exc
        return cls(
            protocol_version=value["protocol_version"],  # type: ignore[arg-type]
            op=operation,
            method=value["method"],  # type: ignore[arg-type]
            path=value["path"],  # type: ignore[arg-type]
            space_id=value["space_id"],  # type: ignore[arg-type]
            source_public_key=value["source_public_key"],  # type: ignore[arg-type]
            source_fingerprint=value["source_fingerprint"],  # type: ignore[arg-type]
            target_fingerprint=value["target_fingerprint"],  # type: ignore[arg-type]
            membership_epoch=value["membership_epoch"],  # type: ignore[arg-type]
            request_id=value["request_id"],  # type: ignore[arg-type]
            nonce=value["nonce"],  # type: ignore[arg-type]
            issued_at_ms=value["issued_at_ms"],  # type: ignore[arg-type]
            body_digest=value["body_digest"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_headers(
        cls, headers: Sequence[tuple[bytes, bytes]]
    ) -> tuple["MeshRequestEnvelope", bytes]:
        envelope_bytes, signature = parse_mesh_headers(headers)
        return cls.from_bytes(envelope_bytes), signature

    def as_dict(self) -> dict[str, object]:
        return {
            "body_digest": self.body_digest,
            "issued_at_ms": self.issued_at_ms,
            "membership_epoch": self.membership_epoch,
            "method": self.method,
            "nonce": self.nonce,
            "op": self.op.value,
            "path": self.path,
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict(), limits=_ENVELOPE_HCJ_LIMITS)

    def sign(self, private_key: MeshPrivateKey) -> bytes:
        if not isinstance(private_key, MeshPrivateKey):
            _fail("invalid_signer", "Mesh signer is invalid")
        if private_key.public_key() != self.source_public_key:
            _fail("signer_identity_mismatch", "Mesh signer identity does not match")
        return private_key.sign(MESH_REQUEST_SIGNATURE_DOMAIN + self.canonical_bytes())

    def verify(self, signature: bytes) -> None:
        _verify_signature(
            self.source_public_key,
            signature,
            MESH_REQUEST_SIGNATURE_DOMAIN + self.canonical_bytes(),
        )

    def bind_request(self, *, method: str, path: str, body: bytes) -> None:
        if type(method) is not str or type(path) is not str or type(body) is not bytes:
            _fail("invalid_request_binding", "Mesh HTTP request binding is invalid")
        if method != self.method or path != self.path:
            _fail("request_binding_mismatch", "Mesh HTTP request binding does not match")
        if self.method == "GET" and body != b"":
            _fail("get_body_forbidden", "Mesh GET requests must have an empty body")
        if body_sha256(body) != self.body_digest:
            _fail("body_digest_mismatch", "Mesh request body digest does not match")


class MeshResponseCode(str, Enum):
    INVALID_EVENT = "INVALID_EVENT"
    LOCAL_UNSAFE = "LOCAL_UNSAFE"
    SOURCE_NOT_AUTHORIZED = "SOURCE_NOT_AUTHORIZED"
    EPOCH_MISMATCH = "EPOCH_MISMATCH"
    REPLAY_REJECTED = "REPLAY_REJECTED"
    OPERATION_UNAVAILABLE = "OPERATION_UNAVAILABLE"
    # P10-3 success codes: the ONLY codes that carry ``acknowledged=True`` and a
    # data body. Every code above stays a refusal with ``acknowledged=False`` so
    # the P10-2 refusal byte-shape is preserved verbatim.
    OK = "OK"
    ACCEPTED = "ACCEPTED"


MESH_RESPONSE_STATUS: Mapping[MeshResponseCode, int] = MappingProxyType(
    {
        MeshResponseCode.INVALID_EVENT: 400,
        MeshResponseCode.LOCAL_UNSAFE: 423,
        MeshResponseCode.SOURCE_NOT_AUTHORIZED: 403,
        MeshResponseCode.EPOCH_MISMATCH: 409,
        MeshResponseCode.REPLAY_REJECTED: 409,
        MeshResponseCode.OPERATION_UNAVAILABLE: 503,
        MeshResponseCode.OK: 200,
        MeshResponseCode.ACCEPTED: 202,
    }
)

#: The success codes that acknowledge and carry a data body (P10-3). All other
#: codes are refusals and MUST remain ``acknowledged=False`` (P10-2 compat).
MESH_SUCCESS_CODES: frozenset[MeshResponseCode] = frozenset(
    {MeshResponseCode.OK, MeshResponseCode.ACCEPTED}
)

_RESPONSE_FIELDS = frozenset(
    {
        "acknowledged",
        "body_digest",
        "code",
        "correlation_id",
        "issued_at_ms",
        "nonce",
        "protocol_version",
        "source_fingerprint",
        "source_public_key",
        "status",
        "target_fingerprint",
    }
)


@dataclass(frozen=True, slots=True)
class MeshResponseEnvelope:
    protocol_version: int
    status: int
    code: MeshResponseCode
    acknowledged: bool
    correlation_id: str
    source_public_key: str
    source_fingerprint: str
    target_fingerprint: str
    issued_at_ms: int
    nonce: str
    body_digest: str

    def __post_init__(self) -> None:
        if type(self.protocol_version) is not int or self.protocol_version != 1:
            _fail("wrong_protocol_version", "Mesh protocol version is incompatible")
        if not isinstance(self.code, MeshResponseCode):
            _fail("invalid_response_code", "Mesh response code is invalid")
        if type(self.status) is not int or self.status != MESH_RESPONSE_STATUS[self.code]:
            _fail("response_status_mismatch", "Mesh response status and code do not match")
        # ``acknowledged`` is bound exactly to the code class: success codes
        # acknowledge (True), every refusal code does not (False). This keeps
        # every P10-2 refusal byte-identical while admitting P10-3 success.
        expected_ack = self.code in MESH_SUCCESS_CODES
        if type(self.acknowledged) is not bool or self.acknowledged is not expected_ack:
            _fail("acknowledgement_mismatch", "Mesh acknowledgement does not match the code")
        _require_plain_str(
            self.correlation_id,
            code="invalid_correlation_id",
            pattern=_REQUEST_ID_RE,
        )
        _validate_source_identity(self.source_public_key, self.source_fingerprint)
        _require_plain_str(
            self.target_fingerprint,
            code="invalid_target_identity",
            pattern=_FINGERPRINT_RE,
        )
        _require_nonnegative_safe_int(self.issued_at_ms, code="invalid_issued_at")
        _require_plain_str(self.nonce, code="invalid_nonce", pattern=_NONCE_RE)
        _require_plain_str(self.body_digest, code="invalid_body_digest", pattern=_DIGEST_RE)

    @classmethod
    def create(
        cls,
        *,
        code: MeshResponseCode,
        correlation_id: str,
        source_public_key: str,
        source_fingerprint: str,
        target_fingerprint: str,
        issued_at_ms: int,
        nonce: str,
        body: bytes,
    ) -> "MeshResponseEnvelope":
        if not isinstance(code, MeshResponseCode):
            _fail("invalid_response_code", "Mesh response code is invalid")
        return cls(
            protocol_version=MESH_PROTOCOL_VERSION,
            status=MESH_RESPONSE_STATUS[code],
            code=code,
            acknowledged=code in MESH_SUCCESS_CODES,
            correlation_id=correlation_id,
            source_public_key=source_public_key,
            source_fingerprint=source_fingerprint,
            target_fingerprint=target_fingerprint,
            issued_at_ms=issued_at_ms,
            nonce=nonce,
            body_digest=body_sha256(body),
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MeshResponseEnvelope":
        try:
            value = canonical_loads(raw, limits=_ENVELOPE_HCJ_LIMITS)
        except HCJError as exc:
            raise MeshWireError("invalid_envelope", "Mesh envelope is invalid") from exc
        if type(value) is not dict or frozenset(value) != _RESPONSE_FIELDS:
            _fail("invalid_envelope_shape", "Mesh response envelope shape is invalid")
        try:
            code = MeshResponseCode(value["code"])
        except (ValueError, TypeError) as exc:
            raise MeshWireError(
                "invalid_response_code", "Mesh response code is invalid"
            ) from exc
        return cls(
            protocol_version=value["protocol_version"],  # type: ignore[arg-type]
            status=value["status"],  # type: ignore[arg-type]
            code=code,
            acknowledged=value["acknowledged"],  # type: ignore[arg-type]
            correlation_id=value["correlation_id"],  # type: ignore[arg-type]
            source_public_key=value["source_public_key"],  # type: ignore[arg-type]
            source_fingerprint=value["source_fingerprint"],  # type: ignore[arg-type]
            target_fingerprint=value["target_fingerprint"],  # type: ignore[arg-type]
            issued_at_ms=value["issued_at_ms"],  # type: ignore[arg-type]
            nonce=value["nonce"],  # type: ignore[arg-type]
            body_digest=value["body_digest"],  # type: ignore[arg-type]
        )

    @classmethod
    def from_headers(
        cls, headers: Sequence[tuple[bytes, bytes]]
    ) -> tuple["MeshResponseEnvelope", bytes]:
        envelope_bytes, signature = parse_mesh_headers(headers)
        return cls.from_bytes(envelope_bytes), signature

    def as_dict(self) -> dict[str, object]:
        return {
            "acknowledged": self.acknowledged,
            "body_digest": self.body_digest,
            "code": self.code.value,
            "correlation_id": self.correlation_id,
            "issued_at_ms": self.issued_at_ms,
            "nonce": self.nonce,
            "protocol_version": self.protocol_version,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "status": self.status,
            "target_fingerprint": self.target_fingerprint,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict(), limits=_ENVELOPE_HCJ_LIMITS)

    def sign(self, private_key: MeshPrivateKey) -> bytes:
        if not isinstance(private_key, MeshPrivateKey):
            _fail("invalid_signer", "Mesh signer is invalid")
        if private_key.public_key() != self.source_public_key:
            _fail("signer_identity_mismatch", "Mesh signer identity does not match")
        return private_key.sign(MESH_RESPONSE_SIGNATURE_DOMAIN + self.canonical_bytes())

    def verify(self, signature: bytes) -> None:
        _verify_signature(
            self.source_public_key,
            signature,
            MESH_RESPONSE_SIGNATURE_DOMAIN + self.canonical_bytes(),
        )

    def bind_response(self, *, status: int, body: bytes) -> None:
        if type(status) is not int or type(body) is not bytes:
            _fail("invalid_response_binding", "Mesh HTTP response binding is invalid")
        if status != self.status or body_sha256(body) != self.body_digest:
            _fail("response_binding_mismatch", "Mesh HTTP response binding does not match")


def _verify_signature(public_key: str, signature: bytes, signed_bytes: bytes) -> None:
    if type(signature) is not bytes or len(signature) != 64:
        _fail("authentication_failed", "Mesh authentication failed")
    try:
        parse_mesh_public_key(public_key).verify(signature, signed_bytes)
    except (InvalidSignature, MeshIdentityError, TypeError, ValueError) as exc:
        raise MeshWireError(
            "authentication_failed", "Mesh authentication failed"
        ) from exc


def _b64url_encode(raw: bytes) -> bytes:
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _b64url_decode(value: bytes, *, max_encoded: int, max_decoded: int) -> bytes:
    if type(value) is not bytes or not value or len(value) > max_encoded:
        _fail("invalid_header", "Mesh authentication headers are invalid")
    if _B64URL_RE.fullmatch(value) is None or b"=" in value:
        _fail("invalid_header", "Mesh authentication headers are invalid")
    try:
        raw = base64.b64decode(
            value + b"=" * (-len(value) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError) as exc:
        raise MeshWireError(
            "invalid_header", "Mesh authentication headers are invalid"
        ) from exc
    if len(raw) > max_decoded or _b64url_encode(raw) != value:
        _fail("invalid_header", "Mesh authentication headers are invalid")
    return raw


def parse_mesh_headers(
    headers: Sequence[tuple[bytes, bytes]],
) -> tuple[bytes, bytes]:
    """Extract exactly one envelope and one signature from raw ASGI headers."""

    if not isinstance(headers, Sequence) or isinstance(headers, (bytes, str)):
        _fail("invalid_headers", "Mesh authentication headers are invalid")
    envelope_values: list[bytes] = []
    signature_values: list[bytes] = []
    for item in headers:
        if type(item) is not tuple or len(item) != 2:
            _fail("invalid_headers", "Mesh authentication headers are invalid")
        name, value = item
        if type(name) is not bytes or type(value) is not bytes:
            _fail("invalid_headers", "Mesh authentication headers are invalid")
        try:
            normalized = name.decode("ascii", "strict").lower().encode("ascii")
        except UnicodeDecodeError:
            # An unrelated non-ASCII header is not a spelling of our ASCII names.
            continue
        if normalized == MESH_ENVELOPE_HEADER:
            envelope_values.append(value)
        elif normalized == MESH_SIGNATURE_HEADER:
            signature_values.append(value)
        elif normalized.startswith(b"hivemind-mesh-"):
            _fail("unknown_mesh_header", "Mesh authentication headers are invalid")

    if len(envelope_values) != 1 or len(signature_values) != 1:
        _fail("header_cardinality", "Mesh authentication headers are invalid")
    envelope_value = envelope_values[0]
    signature_value = signature_values[0]
    # Base64url never contains commas or whitespace; explicitly refusing these
    # also rejects proxy comma-folding and obsolete header line folding.
    if any(byte in envelope_value + signature_value for byte in b", \t\r\n"):
        _fail("folded_header", "Mesh authentication headers are invalid")
    envelope = _b64url_decode(
        envelope_value,
        max_encoded=MAX_ENVELOPE_HEADER_BYTES,
        max_decoded=MAX_ENVELOPE_DECODED_BYTES,
    )
    if len(signature_value) != MAX_SIGNATURE_HEADER_BYTES:
        _fail("invalid_signature_header", "Mesh authentication headers are invalid")
    signature = _b64url_decode(
        signature_value,
        max_encoded=MAX_SIGNATURE_HEADER_BYTES,
        max_decoded=64,
    )
    if len(signature) != 64:
        _fail("invalid_signature_header", "Mesh authentication headers are invalid")
    return envelope, signature


def mesh_headers(
    envelope: MeshRequestEnvelope | MeshResponseEnvelope,
    signature: bytes,
) -> tuple[tuple[bytes, bytes], tuple[bytes, bytes]]:
    """Serialize a validated envelope/signature pair as canonical ASGI headers."""

    if type(envelope) not in (MeshRequestEnvelope, MeshResponseEnvelope):
        _fail("invalid_envelope", "Mesh envelope is invalid")
    if type(signature) is not bytes or len(signature) != 64:
        _fail("invalid_signature", "Mesh signature is invalid")
    encoded_envelope = _b64url_encode(envelope.canonical_bytes())
    if len(encoded_envelope) > MAX_ENVELOPE_HEADER_BYTES:
        _fail("envelope_too_large", "Mesh envelope exceeds the header limit")
    return (
        (MESH_ENVELOPE_HEADER, encoded_envelope),
        (MESH_SIGNATURE_HEADER, _b64url_encode(signature)),
    )


__all__ = [
    "MAX_ENVELOPE_DECODED_BYTES",
    "MAX_ENVELOPE_HEADER_BYTES",
    "MESH_ENVELOPE_HEADER",
    "MESH_PROTOCOL_VERSION",
    "MESH_REQUEST_FRESHNESS_WINDOW_MS",
    "MESH_REQUEST_SIGNATURE_DOMAIN",
    "MESH_RESPONSE_SIGNATURE_DOMAIN",
    "MESH_RESPONSE_STATUS",
    "MESH_ROUTES",
    "MESH_SIGNATURE_HEADER",
    "MESH_TARGET_UNBOUND",
    "MeshHttpOperation",
    "MeshRequestEnvelope",
    "MeshResponseCode",
    "MeshResponseEnvelope",
    "MeshRoute",
    "MeshWireError",
    "body_sha256",
    "mesh_headers",
    "parse_mesh_headers",
]
