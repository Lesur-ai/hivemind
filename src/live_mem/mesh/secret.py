# -*- coding: utf-8 -*-
"""One-time Project Mesh invitation secret handling (P10-3, issue #191).

An interactive invitation carries a **one-time bearer secret** shown to
administrator A exactly once (EPIC #187, PROJECT_MESH.md §3 Action 1).  Only a
**domain-separated hash** of that secret ever enters durable state
(``MeshInvitation.secret_digest``, the pairing session, or any log/audit/DOM):
the raw secret is transport-only (P10_2_WIRE_CONTRACT.md line 161) and is
verified against the stored digest when administrator B's target presents it in
the signed join claim.

The digest is bound to the exact ``pair_id`` and ``space_id`` so a secret can
never be replayed against a different pairing or space.  Fields are
length-framed before hashing so no ``pair_id``/``space_id``/secret boundary is
ambiguous.  This module has no storage, network, or application-state imports.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

#: Domain separator for the invitation-secret hash (never reused elsewhere).
_INVITATION_SECRET_DOMAIN = b"hivemind-mesh-invitation-secret-v1\0"
#: Entropy of a generated one-time secret, in bytes (256 bits).
_SECRET_ENTROPY_BYTES = 32
#: Entropy of a generated pair id / nonce suffix, in bytes.
_PAIR_ID_ENTROPY_BYTES = 16
_NONCE_ENTROPY_BYTES = 32


class MeshSecretError(ValueError):
    """A non-reflective invitation-secret refusal."""

    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message


def generate_invitation_secret() -> str:
    """Return a fresh, high-entropy, URL-safe one-time invitation secret.

    The raw value is shown to the operator exactly once and never persisted; only
    :func:`hash_invitation_secret` output enters durable state.
    """

    return secrets.token_urlsafe(_SECRET_ENTROPY_BYTES)


def generate_pair_id() -> str:
    """Return a fresh ``pair_<32-hex>`` pairing id (matches the wire grammar)."""

    return "pair_" + secrets.token_hex(_PAIR_ID_ENTROPY_BYTES)


def generate_pairing_nonce() -> str:
    """Return a fresh ``nonce_<64-hex>`` artifact nonce (matches the grammar)."""

    return "nonce_" + secrets.token_hex(_NONCE_ENTROPY_BYTES)


def generate_request_id() -> str:
    """Return a fresh ``req_<32-hex>`` transport correlation id (event route)."""

    return "req_" + secrets.token_hex(_PAIR_ID_ENTROPY_BYTES)


def _framed(*parts: bytes) -> bytes:
    # Length-prefix each field (8-byte big-endian) so concatenation is an
    # injective encoding: no crafted pair_id/space_id/secret can collide with a
    # different field split.
    out = bytearray()
    for part in parts:
        out += len(part).to_bytes(8, "big")
        out += part
    return bytes(out)


def hash_invitation_secret(secret: str, *, pair_id: str, space_id: str) -> str:
    """Return the lowercase 64-hex domain-separated digest of an invitation secret.

    The digest binds the secret to ``pair_id`` and ``space_id`` so it cannot be
    replayed against another pairing or space.  Raises :class:`MeshSecretError`
    on empty/invalid input rather than silently hashing degenerate values.
    """

    if type(secret) is not str or not secret:
        raise MeshSecretError("empty_secret", "Mesh invitation secret must be non-empty")
    if type(pair_id) is not str or not pair_id:
        raise MeshSecretError("invalid_pair_id", "Mesh invitation pair id is invalid")
    if type(space_id) is not str or not space_id:
        raise MeshSecretError("invalid_space_id", "Mesh invitation space id is invalid")
    digest = hashlib.sha256()
    digest.update(_INVITATION_SECRET_DOMAIN)
    digest.update(
        _framed(
            pair_id.encode("ascii", "strict"),
            space_id.encode("utf-8", "strict"),
            secret.encode("utf-8", "strict"),
        )
    )
    return digest.hexdigest()


def verify_invitation_secret(
    secret: str, expected_digest: str, *, pair_id: str, space_id: str
) -> bool:
    """Constant-time check that ``secret`` matches ``expected_digest``.

    Returns ``False`` (never raises) for any mismatch, including malformed input,
    so a bad secret is a fail-closed refusal, not an error path an attacker can
    distinguish.
    """

    if type(expected_digest) is not str or len(expected_digest) != 64:
        return False
    try:
        actual = hash_invitation_secret(secret, pair_id=pair_id, space_id=space_id)
    except MeshSecretError:
        return False
    return hmac.compare_digest(actual, expected_digest)


__all__ = [
    "MeshSecretError",
    "generate_invitation_secret",
    "generate_pair_id",
    "generate_pairing_nonce",
    "generate_request_id",
    "hash_invitation_secret",
    "verify_invitation_secret",
]
