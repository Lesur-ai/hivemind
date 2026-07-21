# -*- coding: utf-8 -*-
"""Bounded signed Mesh bootstrap transfer tests (P10-3, issue #191)."""

from __future__ import annotations

import pytest

from live_mem.core.hivemind import BootstrapService, HiveNodeStatus
from live_mem.mesh.bootstrap_snapshot import (
    DEFAULT_BOOTSTRAP_MAX_OBJECTS,
    MeshBootstrapError,
    build_bootstrap,
    import_bootstrap,
    parse_snapshot_payload,
    serialize_snapshot,
)
from live_mem.mesh.identity import generate_mesh_identity
from tests.test_hivemind_pending_import import (
    SOURCE,
    TARGET,
    _seed_blank_target,
    _seed_source_with_pending_peer,
)
from tests.test_hivemind_state import FakeStorage

_SRC_ID = generate_mesh_identity()
_TGT_FP = generate_mesh_identity().fingerprint


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


async def _make_bootstrap(storage):
    _src, tgt_keys = await _seed_source_with_pending_peer(storage)
    await _seed_blank_target(storage)
    svc = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await svc.export_snapshot(SOURCE)
    signed, payload = build_bootstrap(
        snapshot,
        # The envelope space_id is the import destination; in the real protocol the
        # source and target share the space id, so it must equal target_space_id
        # (TARGET here) — the import now binds them (defense in depth).
        space_id=TARGET,
        source_public_key=_SRC_ID.public_key,
        source_fingerprint=_SRC_ID.fingerprint,
        target_fingerprint=_TGT_FP,
        private_key=_SRC_ID.private_key,
    )
    return svc, signed, payload, tgt_keys


async def test_build_then_import_roundtrip_stays_pending_unsafe(storage) -> None:
    svc, signed, payload, tgt_keys = await _make_bootstrap(storage)
    result = await import_bootstrap(
        svc, TARGET,
        signed_envelope=signed, payload=payload, local_keypair=tgt_keys,
        expected_source_public_key=_SRC_ID.public_key, expected_epoch=2,
    )
    assert result.node_status == HiveNodeStatus.UNSAFE
    assert result.membership_epoch == 2


async def test_envelope_signature_verifies_and_rejects_tamper(storage) -> None:
    _svc, signed, _payload, _keys = await _make_bootstrap(storage)
    signed.verify()  # valid
    from dataclasses import replace
    tampered = replace(signed, envelope=replace(signed.envelope, membership_epoch=99))
    with pytest.raises(MeshBootstrapError):
        tampered.verify()


async def test_import_rejects_wrong_source_epoch_and_payload_tamper(storage) -> None:
    svc, signed, payload, tgt_keys = await _make_bootstrap(storage)
    # wrong expected source
    other = generate_mesh_identity()
    with pytest.raises(MeshBootstrapError):
        await import_bootstrap(
            svc, TARGET, signed_envelope=signed, payload=payload, local_keypair=tgt_keys,
            expected_source_public_key=other.public_key, expected_epoch=2,
        )
    # wrong expected epoch
    with pytest.raises(MeshBootstrapError):
        await import_bootstrap(
            svc, TARGET, signed_envelope=signed, payload=payload, local_keypair=tgt_keys,
            expected_source_public_key=_SRC_ID.public_key, expected_epoch=7,
        )
    # tampered payload (digest mismatch)
    with pytest.raises(MeshBootstrapError):
        await import_bootstrap(
            svc, TARGET, signed_envelope=signed, payload=payload + b" ", local_keypair=tgt_keys,
            expected_source_public_key=_SRC_ID.public_key, expected_epoch=2,
        )


async def test_import_rejects_wrong_space(storage) -> None:
    # A source-validly-signed bootstrap for one space must not import into another
    # (defense in depth behind the caller's session-binding check).
    svc, signed, payload, tgt_keys = await _make_bootstrap(storage)  # envelope space_id == TARGET
    with pytest.raises(MeshBootstrapError) as e:
        await import_bootstrap(
            svc, SOURCE,  # import destination != envelope space_id
            signed_envelope=signed, payload=payload, local_keypair=tgt_keys,
            expected_source_public_key=_SRC_ID.public_key, expected_epoch=2,
        )
    assert e.value.code == "wrong_space"


def test_parse_rejects_oversized_payload() -> None:
    raw = b'{"manifest":{},"files":{}}'
    with pytest.raises(MeshBootstrapError) as e:
        parse_snapshot_payload(raw, max_bytes=5)
    assert e.value.code == "too_large"


def test_parse_rejects_big_integer_token() -> None:
    # A >20-digit integer token in the payload must fail closed (not reach int()).
    raw = b'{"files":{},"manifest":{"bank_version":' + b"9" * 5000 + b"}}"
    with pytest.raises(MeshBootstrapError) as e:
        parse_snapshot_payload(raw)
    assert e.value.code == "integer_too_long"


def test_parse_rejects_too_many_objects() -> None:
    files = {f"bank/f{i}.md": "x" for i in range(3)}
    import json
    raw = json.dumps({"manifest": {"entries": []}, "files": files}).encode()
    with pytest.raises(MeshBootstrapError) as e:
        parse_snapshot_payload(raw, max_objects=2)
    assert e.value.code == "too_many_objects"


async def test_serialize_rejects_too_many_objects(storage) -> None:
    _src, _tgt = await _seed_source_with_pending_peer(storage)
    svc = BootstrapService(storage)  # type: ignore[arg-type]
    snapshot = await svc.export_snapshot(SOURCE)
    with pytest.raises(MeshBootstrapError):
        serialize_snapshot(snapshot, max_objects=1)  # source has >1 shared file
