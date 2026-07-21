# -*- coding: utf-8 -*-
"""Strict, local-only Ed25519 identity primitives for Project Mesh."""

from __future__ import annotations

import base64
import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


MESH_PRIVATE_KEY_PREFIX: Final = "ed25519-private:v1:"
MESH_PUBLIC_KEY_PREFIX: Final = "ed25519-public:v1:"
LEGACY_MEMBERSHIP_PUBLIC_KEY_PREFIX: Final = "ed25519:"
MESH_FINGERPRINT_PREFIX: Final = "hm1:"

_FINGERPRINT_DOMAIN: Final = b"hivemind-mesh-identity-v1\0"
_RAW_KEY_BYTES: Final = 32
_B64URL_32_RE: Final = re.compile(r"^[A-Za-z0-9_-]{43}$")


class MeshIdentityError(ValueError):
    """A Mesh identity value is invalid without echoing credential input."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_canonical_key(value: object, prefix: str, label: str) -> bytes:
    if not isinstance(value, str) or not value.startswith(prefix):
        raise MeshIdentityError(f"invalid {label} encoding")
    payload = value[len(prefix) :]
    if _B64URL_32_RE.fullmatch(payload) is None:
        raise MeshIdentityError(f"invalid {label} encoding")
    try:
        raw = base64.b64decode(payload + "=", altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise MeshIdentityError(f"invalid {label} encoding") from None
    if len(raw) != _RAW_KEY_BYTES or _b64url_encode(raw) != payload:
        raise MeshIdentityError(f"invalid {label} encoding")
    return raw


class MeshPrivateKey:
    """Opaque signing capability; plaintext export is intentionally private.

    It is not a Pydantic model, exposes no public byte/string serialization,
    has no ``__dict__``, and refuses copy/deepcopy/pickle protocols.  Runtime
    consumers receive only the signing operation and the derived public key.
    """

    __slots__ = ("__key",)

    def __init__(self, key: Ed25519PrivateKey) -> None:
        if not isinstance(key, Ed25519PrivateKey):
            raise TypeError("MeshPrivateKey requires an Ed25519 private key")
        self.__key = key

    def __repr__(self) -> str:
        return "<MeshPrivateKey redacted>"

    __str__ = __repr__

    def __copy__(self) -> "MeshPrivateKey":
        raise TypeError("MeshPrivateKey cannot be copied")

    def __deepcopy__(self, memo: object) -> "MeshPrivateKey":
        del memo
        raise TypeError("MeshPrivateKey cannot be copied")

    def __reduce__(self) -> object:
        raise TypeError("MeshPrivateKey cannot be pickled")

    def __reduce_ex__(self, protocol: int) -> object:
        del protocol
        raise TypeError("MeshPrivateKey cannot be pickled")

    def __getstate__(self) -> object:
        raise TypeError("MeshPrivateKey cannot be serialized")

    def sign(self, payload: bytes) -> bytes:
        """Sign bytes without exposing private material."""

        if not isinstance(payload, bytes):
            raise TypeError("MeshPrivateKey.sign payload must be bytes")
        return self.__key.sign(payload)

    def public_key(self) -> str:
        """Return the strict Mesh-v1 public-key encoding."""

        raw = self.__key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return MESH_PUBLIC_KEY_PREFIX + _b64url_encode(raw)

@dataclass(frozen=True, slots=True)
class MeshIdentity:
    """Generated local identity with an opaque private signing capability."""

    public_key: str
    fingerprint: str
    private_key: MeshPrivateKey = field(repr=False, compare=False)

    def __repr__(self) -> str:
        return (
            "MeshIdentity("
            f"public_key={self.public_key!r}, "
            f"fingerprint={self.fingerprint!r}, "
            "private_key=<redacted>)"
        )

    def __copy__(self) -> "MeshIdentity":
        raise TypeError("MeshIdentity containing a private key cannot be copied")

    def __deepcopy__(self, memo: object) -> "MeshIdentity":
        del memo
        raise TypeError("MeshIdentity containing a private key cannot be copied")


def parse_mesh_private_key(encoded_private_key: object) -> MeshPrivateKey:
    """Parse only the strict ``ed25519-private:v1`` deployment format."""

    raw = _decode_canonical_key(
        encoded_private_key,
        MESH_PRIVATE_KEY_PREFIX,
        "Mesh private key",
    )
    try:
        return MeshPrivateKey(Ed25519PrivateKey.from_private_bytes(raw))
    except ValueError:
        raise MeshIdentityError("invalid Mesh private key encoding") from None


def parse_mesh_public_key(encoded_public_key: object) -> Ed25519PublicKey:
    """Parse only the strict ``ed25519-public:v1`` wire/config format."""

    raw = decode_mesh_public_key(encoded_public_key)
    try:
        return Ed25519PublicKey.from_public_bytes(raw)
    except ValueError:
        raise MeshIdentityError("invalid Mesh public key encoding") from None


def decode_mesh_public_key(encoded_public_key: object) -> bytes:
    """Decode only strict Mesh-v1 public material for wire/config consumers."""

    return _decode_canonical_key(
        encoded_public_key,
        MESH_PUBLIC_KEY_PREFIX,
        "Mesh public key",
    )


def decode_membership_public_key(encoded_public_key: object) -> bytes:
    """Decode public material for raw membership equality only.

    This narrow compatibility seam accepts the historic membership encoding
    and strict Mesh-v1 public encoding.  It must not be used by Mesh config or
    wire parsing and deliberately performs no rewrite, persistence, or key
    rotation.
    """

    if isinstance(encoded_public_key, str) and encoded_public_key.startswith(
        LEGACY_MEMBERSHIP_PUBLIC_KEY_PREFIX
    ):
        return _decode_canonical_key(
            encoded_public_key,
            LEGACY_MEMBERSHIP_PUBLIC_KEY_PREFIX,
            "membership public key",
        )
    return _decode_canonical_key(
        encoded_public_key,
        MESH_PUBLIC_KEY_PREFIX,
        "membership public key",
    )


def mesh_public_key_from_private(private_key: MeshPrivateKey) -> str:
    """Derive the strict public encoding from an opaque private key."""

    if not isinstance(private_key, MeshPrivateKey):
        raise TypeError("private_key must be MeshPrivateKey")
    return private_key.public_key()


def mesh_identity_fingerprint(encoded_public_key: object) -> str:
    """Return ``hm1:`` + SHA-256(domain separator + raw public key)."""

    raw = decode_mesh_public_key(encoded_public_key)
    digest = hashlib.sha256(_FINGERPRINT_DOMAIN + raw).hexdigest()
    return MESH_FINGERPRINT_PREFIX + digest


def generate_mesh_identity() -> MeshIdentity:
    """Generate an Ed25519 Mesh identity without returning plaintext secret."""

    private_key = MeshPrivateKey(Ed25519PrivateKey.generate())
    public_key = private_key.public_key()
    return MeshIdentity(
        public_key=public_key,
        fingerprint=mesh_identity_fingerprint(public_key),
        private_key=private_key,
    )


def _write_generated_mesh_identity(fd: int) -> MeshIdentity:
    """Generate an identity and write its secret encoding to a secured fd.

    This private CLI-only seam intentionally never returns plaintext secret
    bytes or adds a serialization operation to :class:`MeshPrivateKey`.
    The caller owns descriptor validation, fsync, and failure cleanup.
    """

    if not isinstance(fd, int) or fd < 0:
        raise TypeError("fd must be an open file descriptor")
    cryptography_key = Ed25519PrivateKey.generate()
    raw = cryptography_key.private_bytes(
        Encoding.Raw,
        PrivateFormat.Raw,
        NoEncryption(),
    )
    encoded = (MESH_PRIVATE_KEY_PREFIX + _b64url_encode(raw)).encode("ascii")
    view = memoryview(encoded)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while creating key output")
        view = view[written:]

    private_key = MeshPrivateKey(cryptography_key)
    public_key = private_key.public_key()
    return MeshIdentity(
        public_key=public_key,
        fingerprint=mesh_identity_fingerprint(public_key),
        private_key=private_key,
    )
