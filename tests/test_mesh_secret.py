# -*- coding: utf-8 -*-
"""One-time Mesh invitation secret tests (P10-3, issue #191)."""

from __future__ import annotations

import re

import pytest

from live_mem.mesh.secret import (
    MeshSecretError,
    generate_invitation_secret,
    generate_pair_id,
    generate_pairing_nonce,
    hash_invitation_secret,
    verify_invitation_secret,
)

_PAIR = "pair_" + "a" * 32
_OTHER_PAIR = "pair_" + "b" * 32
_SPACE = "mesh-test-space"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def test_generated_secret_is_high_entropy_and_unique() -> None:
    values = {generate_invitation_secret() for _ in range(64)}
    assert len(values) == 64  # no collisions
    for value in values:
        assert len(value) >= 40  # ~256 bits url-safe


def test_generated_pair_id_and_nonce_match_wire_grammar() -> None:
    assert re.fullmatch(r"pair_[0-9a-f]{32}", generate_pair_id())
    assert re.fullmatch(r"nonce_[0-9a-f]{64}", generate_pairing_nonce())
    assert generate_pair_id() != generate_pair_id()


def test_hash_is_lowercase_64_hex_and_deterministic() -> None:
    secret = "s3cr3t-value"
    d1 = hash_invitation_secret(secret, pair_id=_PAIR, space_id=_SPACE)
    d2 = hash_invitation_secret(secret, pair_id=_PAIR, space_id=_SPACE)
    assert _DIGEST_RE.fullmatch(d1)
    assert d1 == d2  # deterministic


def test_hash_is_bound_to_pair_id_and_space_id() -> None:
    secret = generate_invitation_secret()
    base = hash_invitation_secret(secret, pair_id=_PAIR, space_id=_SPACE)
    # Same secret, different pairing -> different digest (no cross-pairing replay).
    assert hash_invitation_secret(secret, pair_id=_OTHER_PAIR, space_id=_SPACE) != base
    # Same secret, different space -> different digest (no cross-space replay).
    assert hash_invitation_secret(secret, pair_id=_PAIR, space_id="other-space") != base


def test_framing_prevents_boundary_ambiguity() -> None:
    # Without length-framing, ("ab","c") and ("a","bc") could collide. Framing
    # must keep these distinct across the pair_id/space_id boundary.
    d1 = hash_invitation_secret("x", pair_id="pair_" + "a" * 31 + "b", space_id="c")
    d2 = hash_invitation_secret("x", pair_id="pair_" + "a" * 32, space_id="bc"[1:] + "c")
    assert d1 != d2


def test_verify_accepts_correct_and_rejects_wrong() -> None:
    secret = generate_invitation_secret()
    digest = hash_invitation_secret(secret, pair_id=_PAIR, space_id=_SPACE)
    assert verify_invitation_secret(secret, digest, pair_id=_PAIR, space_id=_SPACE) is True
    assert verify_invitation_secret("wrong", digest, pair_id=_PAIR, space_id=_SPACE) is False
    assert verify_invitation_secret(secret, digest, pair_id=_OTHER_PAIR, space_id=_SPACE) is False
    assert verify_invitation_secret(secret, digest, pair_id=_PAIR, space_id="other") is False


def test_verify_is_fail_closed_on_malformed_digest_or_secret() -> None:
    assert verify_invitation_secret("s", "not-a-digest", pair_id=_PAIR, space_id=_SPACE) is False
    assert verify_invitation_secret("s", "a" * 63, pair_id=_PAIR, space_id=_SPACE) is False
    # Empty secret never raises through verify.
    assert verify_invitation_secret("", "a" * 64, pair_id=_PAIR, space_id=_SPACE) is False


def test_hash_rejects_empty_inputs() -> None:
    with pytest.raises(MeshSecretError):
        hash_invitation_secret("", pair_id=_PAIR, space_id=_SPACE)
    with pytest.raises(MeshSecretError):
        hash_invitation_secret("s", pair_id="", space_id=_SPACE)
    with pytest.raises(MeshSecretError):
        hash_invitation_secret("s", pair_id=_PAIR, space_id="")
