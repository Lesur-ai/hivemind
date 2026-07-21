# -*- coding: utf-8 -*-
"""HCJ-1 canonical JSON hardening tests (P10-3, issue #191).

P10-3 ingests signed enrollment/bootstrap artifacts from untrusted bytes through
the wide-limit ``from_bytes`` helpers.  CPython caps integer<->string conversion
at ``sys.get_int_max_str_digits()`` (4300 by default) and raises a *bare*
``ValueError`` for a longer token.  Before this hardening that ``ValueError``
escaped :func:`canonical_loads` un-normalized and propagated as an unhandled
exception out of the artifact/replay parsers.  These tests pin that an oversized
integer token now fails closed as a machine-readable :class:`HCJError`
(``integer_too_long``), distinct from a normal out-of-range integer
(``integer_out_of_range``), on every ingest path.
"""

from __future__ import annotations

import sys

import pytest

from live_mem.mesh.canonical import (
    HCJError,
    MAX_SAFE_INTEGER,
    canonical_loads,
    canonical_sha256,
)


def _oversized_int_digits() -> int:
    # Exceed CPython's integer-string-conversion digit limit with margin.
    return max(sys.get_int_max_str_digits(), 4300) + 1000


def test_oversized_integer_token_fails_closed_as_hcjerror() -> None:
    raw = b"9" * _oversized_int_digits()
    with pytest.raises(HCJError) as excinfo:
        canonical_loads(raw)
    assert excinfo.value.code == "integer_too_long"
    # The giant token must never appear in the safe (non-reflective) message.
    assert "9" * 32 not in excinfo.value.safe_message


def test_oversized_integer_inside_object_fails_closed() -> None:
    raw = b'{"n":' + b"9" * _oversized_int_digits() + b"}"
    with pytest.raises(HCJError) as excinfo:
        canonical_loads(raw)
    assert excinfo.value.code == "integer_too_long"


def test_canonical_sha256_bytes_path_normalizes_oversized_integer() -> None:
    raw = b"9" * _oversized_int_digits()
    with pytest.raises(HCJError) as excinfo:
        canonical_sha256(raw)
    assert excinfo.value.code == "integer_too_long"


def test_normal_out_of_range_integer_keeps_its_distinct_code() -> None:
    # A few-digit integer above 2^53-1 is well under the digit limit: it must
    # still be labelled integer_out_of_range, proving the new try/except is
    # scoped to int() only and does not swallow the range check.
    raw = str(MAX_SAFE_INTEGER + 1).encode("ascii")
    with pytest.raises(HCJError) as excinfo:
        canonical_loads(raw)
    assert excinfo.value.code == "integer_out_of_range"


def test_safe_integer_boundaries_still_parse() -> None:
    assert canonical_loads(str(MAX_SAFE_INTEGER).encode("ascii")) == MAX_SAFE_INTEGER
    assert canonical_loads(str(-MAX_SAFE_INTEGER).encode("ascii")) == -MAX_SAFE_INTEGER
    assert canonical_loads(b"0") == 0


def test_artifact_from_bytes_normalizes_oversized_integer() -> None:
    # The wide-limit artifact ingest path must fail closed as a typed refusal,
    # never leak a *bare* ValueError, on an oversized integer token.
    from live_mem.mesh.artifacts import MeshArtifactError, SignedMeshArtifact

    raw = b'{"artifact":{"n":' + b"9" * _oversized_int_digits() + b'},"signature":"x"}'
    with pytest.raises(Exception) as excinfo:
        SignedMeshArtifact.from_bytes(raw)
    assert isinstance(excinfo.value, (HCJError, MeshArtifactError))
    assert type(excinfo.value) is not ValueError


def test_replay_record_from_bytes_normalizes_oversized_integer() -> None:
    from live_mem.mesh.replay import ReplayError, ReplayRecord

    raw = b'{"issued_at_ms":' + b"9" * _oversized_int_digits() + b"}"
    with pytest.raises(Exception) as excinfo:
        ReplayRecord.from_bytes(raw)
    # It must fail closed as a typed refusal, not escape as a bare ValueError.
    assert isinstance(excinfo.value, (HCJError, ReplayError))
    assert type(excinfo.value) is not ValueError
