# -*- coding: utf-8 -*-
"""Bounded, signed Project Mesh bootstrap transfer (P10-3, issue #191).

P10-2 provides only a raw, size-bounded byte stream (``MeshBootstrapStream``,
256 MiB) with no object model.  P10-3 adds the signed ``MeshBootstrapEnvelope``
metadata and the **bounded** payload (de)serialization that turns that stream
into a ``BootstrapSnapshot`` importable by
``BootstrapService.import_pending_snapshot``.

Two size domains:

* the **envelope** is small structured metadata serialized as HCJ-1 (canonical,
  hardened big-int parser) and signed by the source instance key;
* the **payload** (manifest + per-file contents) is plain JSON — bank/live file
  bodies far exceed HCJ's per-string limits — parsed with an explicit
  ``max_bytes`` cap, an ``object_count`` cap (50,000), and a bounded integer
  hook, so an oversized or big-integer payload fails closed **before** any model
  expansion or delegation to the import transaction (self-review finding M7).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..core.hivemind import (
    BootstrapManifest,
    BootstrapSnapshot,
    ImportResult,
)
from .canonical import canonical_dumps, canonical_loads
from .identity import MeshIdentityError, MeshPrivateKey, parse_mesh_public_key

_FINGERPRINT_RE = re.compile(r"^hm1:[0-9a-f]{64}$", re.ASCII)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_PUBLIC_KEY_RE = re.compile(r"^ed25519-public:v1:[A-Za-z0-9_-]{43}$", re.ASCII)
_SPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", re.ASCII)

_BOOTSTRAP_ENVELOPE_DOMAIN = b"hivemind-mesh-bootstrap-v1\0"
_PROTOCOL_VERSION = 1
#: Bootstrap size/count safety caps (mirror the frozen config defaults).
DEFAULT_BOOTSTRAP_MAX_BYTES = 268_435_456
DEFAULT_BOOTSTRAP_MAX_OBJECTS = 50_000
#: Bootstrap metadata integers (sizes/epochs/versions) are small; reject any
#: token longer than this so an oversized integer fails closed before int().
_MAX_INT_DIGITS = 20


class MeshBootstrapError(ValueError):
    """Non-reflective bootstrap refusal."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise MeshBootstrapError(code, message)


def _bounded_int(raw: str) -> int:
    if len(raw.lstrip("-")) > _MAX_INT_DIGITS:
        raise MeshBootstrapError("integer_too_long", "bootstrap integer token is too long")
    return int(raw)


def _reject_float(_raw: str) -> Any:
    raise MeshBootstrapError("float_forbidden", "bootstrap payload must not contain floats")


def payload_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(value: str, *, expected_len: int) -> bytes:
    if type(value) is not str:
        raise MeshBootstrapError("invalid_signature", "signature must be a string")
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise MeshBootstrapError("invalid_signature", "signature is not valid base64url") from exc
    if len(raw) != expected_len:
        raise MeshBootstrapError("invalid_signature", "signature has the wrong length")
    return raw


@dataclass(frozen=True, slots=True)
class MeshBootstrapEnvelope:
    """Signed metadata describing a bootstrap payload transferred to a target."""

    protocol_version: int
    source_public_key: str
    source_fingerprint: str
    target_fingerprint: str
    space_id: str
    membership_epoch: int
    bank_version: int
    manifest_digest: str
    object_count: int
    payload_digest: str

    def __post_init__(self) -> None:
        _require(self.protocol_version == _PROTOCOL_VERSION, "invalid_version", "unsupported version")
        _require(_PUBLIC_KEY_RE.fullmatch(self.source_public_key) is not None, "invalid_key", "invalid source key")
        _require(_FINGERPRINT_RE.fullmatch(self.source_fingerprint) is not None, "invalid_fp", "invalid source fp")
        _require(_FINGERPRINT_RE.fullmatch(self.target_fingerprint) is not None, "invalid_fp", "invalid target fp")
        _require(_SPACE_ID_RE.fullmatch(self.space_id) is not None, "invalid_space", "invalid space id")
        _require(type(self.membership_epoch) is int and self.membership_epoch >= 0, "invalid_epoch", "invalid epoch")
        _require(type(self.bank_version) is int and self.bank_version >= -1, "invalid_bv", "invalid bank version")
        _require(_DIGEST_RE.fullmatch(self.manifest_digest) is not None, "invalid_digest", "invalid manifest digest")
        _require(
            type(self.object_count) is int and 0 <= self.object_count <= DEFAULT_BOOTSTRAP_MAX_OBJECTS,
            "invalid_count", "invalid object count",
        )
        _require(_DIGEST_RE.fullmatch(self.payload_digest) is not None, "invalid_digest", "invalid payload digest")

    def as_dict(self) -> dict[str, Any]:
        return {
            "bank_version": self.bank_version,
            "manifest_digest": self.manifest_digest,
            "membership_epoch": self.membership_epoch,
            "object_count": self.object_count,
            "payload_digest": self.payload_digest,
            "protocol_version": self.protocol_version,
            "source_fingerprint": self.source_fingerprint,
            "source_public_key": self.source_public_key,
            "space_id": self.space_id,
            "target_fingerprint": self.target_fingerprint,
        }

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MeshBootstrapEnvelope":
        _require(isinstance(data, Mapping), "invalid_envelope", "envelope must be a mapping")
        try:
            return cls(
                protocol_version=data["protocol_version"],
                source_public_key=data["source_public_key"],
                source_fingerprint=data["source_fingerprint"],
                target_fingerprint=data["target_fingerprint"],
                space_id=data["space_id"],
                membership_epoch=data["membership_epoch"],
                bank_version=data["bank_version"],
                manifest_digest=data["manifest_digest"],
                object_count=data["object_count"],
                payload_digest=data["payload_digest"],
            )
        except KeyError as exc:
            raise MeshBootstrapError("missing_field", "bootstrap envelope is missing a field") from exc


@dataclass(frozen=True, slots=True)
class SignedMeshBootstrapEnvelope:
    """A ``MeshBootstrapEnvelope`` plus the source's detached signature."""

    envelope: MeshBootstrapEnvelope
    signature: str

    @classmethod
    def sign(cls, envelope: MeshBootstrapEnvelope, private_key: MeshPrivateKey) -> "SignedMeshBootstrapEnvelope":
        sig = private_key.sign(_BOOTSTRAP_ENVELOPE_DOMAIN + envelope.canonical_bytes())
        # bind the signer public key to the declared source key
        if private_key.public_key() != envelope.source_public_key:
            raise MeshBootstrapError("key_mismatch", "signer key does not match envelope source key")
        return cls(envelope=envelope, signature=_b64url_no_pad(sig))

    def verify(self) -> None:
        """Raise unless the signature is valid for the envelope's source key."""

        try:
            verifier = parse_mesh_public_key(self.envelope.source_public_key)
        except MeshIdentityError as exc:
            raise MeshBootstrapError("invalid_key", "invalid source key") from exc
        raw = _b64url_decode(self.signature, expected_len=64)
        try:
            verifier.verify(raw, _BOOTSTRAP_ENVELOPE_DOMAIN + self.envelope.canonical_bytes())
        except Exception as exc:
            raise MeshBootstrapError("bad_signature", "bootstrap envelope signature is invalid") from exc

    def as_dict(self) -> dict[str, Any]:
        return {"envelope": self.envelope.as_dict(), "signature": self.signature}

    def canonical_bytes(self) -> bytes:
        return canonical_dumps(self.as_dict())

    @classmethod
    def from_bytes(cls, raw: bytes) -> "SignedMeshBootstrapEnvelope":
        data = canonical_loads(raw)
        if not isinstance(data, Mapping) or "envelope" not in data or "signature" not in data:
            raise MeshBootstrapError("invalid_envelope", "signed bootstrap envelope is malformed")
        sig = data["signature"]
        if type(sig) is not str:
            raise MeshBootstrapError("invalid_signature", "signature must be a string")
        return cls(envelope=MeshBootstrapEnvelope.from_dict(data["envelope"]), signature=sig)


def serialize_snapshot(
    snapshot: BootstrapSnapshot,
    *,
    max_objects: int = DEFAULT_BOOTSTRAP_MAX_OBJECTS,
    max_bytes: int = DEFAULT_BOOTSTRAP_MAX_BYTES,
) -> bytes:
    """Serialize a snapshot to bounded plain-JSON payload bytes.

    Fails closed if the file/entry count exceeds ``max_objects`` or the encoded
    size exceeds ``max_bytes`` BEFORE returning anything.
    """

    files = snapshot.files
    manifest = snapshot.manifest
    _require(
        len(files) <= max_objects and len(manifest.entries) <= max_objects,
        "too_many_objects", "bootstrap exceeds the object-count bound",
    )
    body = {
        "manifest": manifest.model_dump(mode="json"),
        "files": dict(files),
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _require(len(raw) <= max_bytes, "too_large", "bootstrap exceeds the byte bound")
    return raw


def parse_snapshot_payload(
    raw: bytes,
    *,
    max_objects: int = DEFAULT_BOOTSTRAP_MAX_OBJECTS,
    max_bytes: int = DEFAULT_BOOTSTRAP_MAX_BYTES,
) -> BootstrapSnapshot:
    """Parse bounded bootstrap payload bytes into a ``BootstrapSnapshot``.

    Enforces ``max_bytes`` and ``max_objects`` and a bounded-integer hook BEFORE
    any pydantic model expansion, so a malicious/oversized payload fails closed.
    """

    _require(type(raw) is bytes, "invalid_input", "bootstrap payload must be bytes")
    _require(len(raw) <= max_bytes, "too_large", "bootstrap exceeds the byte bound")
    try:
        text = raw.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise MeshBootstrapError("invalid_utf8", "bootstrap payload is not valid UTF-8") from exc
    try:
        body = json.loads(text, parse_int=_bounded_int, parse_float=_reject_float)
    except MeshBootstrapError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise MeshBootstrapError("invalid_json", "bootstrap payload is not valid JSON") from exc
    _require(isinstance(body, dict) and set(body) == {"manifest", "files"}, "invalid_shape", "bootstrap payload shape is invalid")
    manifest_raw = body["manifest"]
    files = body["files"]
    _require(isinstance(manifest_raw, dict), "invalid_manifest", "bootstrap manifest is invalid")
    _require(isinstance(files, dict), "invalid_files", "bootstrap files are invalid")
    _require(len(files) <= max_objects, "too_many_objects", "bootstrap exceeds the object-count bound")
    entries = manifest_raw.get("entries")
    _require(isinstance(entries, list) and len(entries) <= max_objects, "too_many_objects", "bootstrap manifest exceeds the object-count bound")
    for key, value in files.items():
        _require(type(key) is str and type(value) is str, "invalid_files", "bootstrap file entries must be strings")
    try:
        manifest = BootstrapManifest.model_validate(manifest_raw)
    except Exception as exc:  # pydantic ValidationError
        raise MeshBootstrapError("invalid_manifest", "bootstrap manifest failed validation") from exc
    return BootstrapSnapshot(manifest=manifest, files=dict(files))


def build_bootstrap(
    snapshot: BootstrapSnapshot,
    *,
    space_id: str,
    source_public_key: str,
    source_fingerprint: str,
    target_fingerprint: str,
    private_key: MeshPrivateKey,
    max_objects: int = DEFAULT_BOOTSTRAP_MAX_OBJECTS,
    max_bytes: int = DEFAULT_BOOTSTRAP_MAX_BYTES,
) -> tuple[SignedMeshBootstrapEnvelope, bytes]:
    """Build a signed envelope + bounded payload bytes from a snapshot."""

    payload = serialize_snapshot(snapshot, max_objects=max_objects, max_bytes=max_bytes)
    manifest = snapshot.manifest
    envelope = MeshBootstrapEnvelope(
        protocol_version=_PROTOCOL_VERSION,
        source_public_key=source_public_key,
        source_fingerprint=source_fingerprint,
        target_fingerprint=target_fingerprint,
        space_id=space_id,
        membership_epoch=manifest.membership_epoch,
        bank_version=manifest.bank_version,
        manifest_digest=manifest.manifest_sha256,
        object_count=len(manifest.entries),
        payload_digest=payload_digest(payload),
    )
    return SignedMeshBootstrapEnvelope.sign(envelope, private_key), payload


async def import_bootstrap(
    bootstrap_service: Any,
    target_space_id: str,
    *,
    signed_envelope: SignedMeshBootstrapEnvelope,
    payload: bytes,
    local_keypair: Any,
    expected_source_public_key: str,
    expected_epoch: int,
    max_objects: int = DEFAULT_BOOTSTRAP_MAX_OBJECTS,
    max_bytes: int = DEFAULT_BOOTSTRAP_MAX_BYTES,
) -> ImportResult:
    """Verify + bounded-parse a bootstrap and import it fail-closed as PENDING.

    Verifies the envelope signature and that it was signed by the EXPECTED source
    (never trusting the wire), that the payload digest and object bounds hold,
    and that the parsed manifest agrees with the signed envelope, before
    delegating to ``BootstrapService.import_pending_snapshot`` (which keeps the
    target UNSAFE until its e+2 self-activation).
    """

    signed_envelope.verify()
    env = signed_envelope.envelope
    _require(env.source_public_key == expected_source_public_key, "wrong_source", "bootstrap source is not the expected peer")
    _require(env.membership_epoch == expected_epoch, "wrong_epoch", "bootstrap epoch mismatch")
    # Defense in depth: a valid signature is not consent to import into ANY space.
    # Bind the envelope to the exact target space so a validly-signed bootstrap for
    # another space cannot be imported into this reserved target (the caller also
    # binds source/target fingerprints against its session before reaching here).
    _require(env.space_id == target_space_id, "wrong_space", "bootstrap space is not the target space")
    _require(payload_digest(payload) == env.payload_digest, "payload_mismatch", "bootstrap payload digest mismatch")
    snapshot = parse_snapshot_payload(payload, max_objects=max_objects, max_bytes=max_bytes)
    _require(snapshot.manifest.manifest_sha256 == env.manifest_digest, "manifest_mismatch", "bootstrap manifest digest mismatch")
    _require(snapshot.manifest.membership_epoch == env.membership_epoch, "epoch_mismatch", "bootstrap manifest epoch mismatch")
    return await bootstrap_service.import_pending_snapshot(target_space_id, snapshot, local_keypair)


__all__ = [
    "MeshBootstrapError",
    "MeshBootstrapEnvelope",
    "SignedMeshBootstrapEnvelope",
    "serialize_snapshot",
    "parse_snapshot_payload",
    "build_bootstrap",
    "import_bootstrap",
    "payload_digest",
    "DEFAULT_BOOTSTRAP_MAX_BYTES",
    "DEFAULT_BOOTSTRAP_MAX_OBJECTS",
]
