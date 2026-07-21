# -*- coding: utf-8 -*-
"""P10-2 canonical wire and durable transport-replay security contract."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import os
import stat
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from live_mem.core.secret_fs import SecretFilesystemError
from live_mem.mesh import replay as mesh_replay
from live_mem.mesh.canonical import (
    HCJError,
    HCJLimits,
    MAX_SAFE_INTEGER,
    canonical_dumps,
    canonical_loads,
    canonical_sha256,
)
from live_mem.mesh.identity import (
    MeshPrivateKey,
    mesh_identity_fingerprint,
    parse_mesh_public_key,
)
from live_mem.mesh.replay import (
    PROCESS_LOCK_DOMAIN,
    REPLAY_KEY_DOMAIN,
    DurableReplayLedger,
    MeshProcessIdentityLock,
    ReplayDecision,
    ReplayError,
    TRANSPORT_REPLAY_TTL_MS,
)
from live_mem.mesh.router import FRESHNESS_WINDOW_MS
from live_mem.mesh.wire import (
    MAX_ENVELOPE_DECODED_BYTES,
    MAX_ENVELOPE_HEADER_BYTES,
    MESH_ENVELOPE_HEADER,
    MESH_REQUEST_FRESHNESS_WINDOW_MS,
    MESH_REQUEST_SIGNATURE_DOMAIN,
    MESH_RESPONSE_SIGNATURE_DOMAIN,
    MESH_RESPONSE_STATUS,
    MESH_ROUTES,
    MESH_SIGNATURE_HEADER,
    MeshHttpOperation,
    MeshRequestEnvelope,
    MeshResponseCode,
    MeshResponseEnvelope,
    MeshWireError,
    body_sha256,
    mesh_headers,
    parse_mesh_headers,
)


def _private(label: str) -> MeshPrivateKey:
    seed = hashlib.sha256(("mesh-test:" + label).encode("ascii")).digest()
    return MeshPrivateKey(Ed25519PrivateKey.from_private_bytes(seed))


def _identity(label: str) -> tuple[MeshPrivateKey, str, str]:
    private = _private(label)
    public = private.public_key()
    return private, public, mesh_identity_fingerprint(public)


def _event_request(
    label: str = "source",
    *,
    target_label: str = "target",
    nonce_hex: str = "1",
    request_hex: str = "2",
    issued_at_ms: int = 1_000,
    body: bytes = b'{"Payload":"transport-only"}',
) -> tuple[MeshPrivateKey, MeshRequestEnvelope, bytes]:
    private, public, fingerprint = _identity(label)
    _, _, target_fingerprint = _identity(target_label)
    envelope = MeshRequestEnvelope.create(
        op=MeshHttpOperation.EVENTS,
        path="/mesh/v1/events",
        space_id="alpha",
        source_public_key=public,
        source_fingerprint=fingerprint,
        target_fingerprint=target_fingerprint,
        membership_epoch=7,
        request_id="req_" + request_hex * 32,
        nonce="nonce_" + nonce_hex * 64,
        issued_at_ms=issued_at_ms,
        body=body,
    )
    return private, envelope, body


def _pair_request(
    operation: MeshHttpOperation = MeshHttpOperation.PAIR_STATUS,
) -> MeshRequestEnvelope:
    private, public, fingerprint = _identity("pair-source")
    del private
    _, _, target_fingerprint = _identity("pair-target")
    pair_id = "pair_" + "a" * 32
    route = MESH_ROUTES[operation]
    body = b"" if route.method == "GET" else b"{}"
    return MeshRequestEnvelope.create(
        op=operation,
        path=route.path_for(pair_id if route.pair_id_in_path else None),
        space_id="alpha",
        source_public_key=public,
        source_fingerprint=fingerprint,
        target_fingerprint=target_fingerprint,
        membership_epoch=7,
        request_id=pair_id,
        nonce="nonce_" + "a" * 64,
        issued_at_ms=1_000,
        body=body,
    )


def _prefix(label: str) -> str:
    _, _, fingerprint = _identity("local-" + label)
    return f"_system/mesh_pairing/{fingerprint}/replay/"


def _replay_expiry(envelope: MeshRequestEnvelope) -> int:
    return envelope.issued_at_ms + TRANSPORT_REPLAY_TTL_MS


class _StringSubclass(str):
    pass


class _IntSubclass(int):
    pass


def test_hcj_known_vector_and_digest_are_exact() -> None:
    value = {
        "Z": "é",
        "A": [None, True, False, -MAX_SAFE_INTEGER, MAX_SAFE_INTEGER],
        "a": 1,
    }
    expected = (
        '{"A":[null,true,false,-9007199254740991,9007199254740991],'
        '"Z":"é","a":1}'
    ).encode("utf-8")

    assert canonical_dumps(value) == expected
    assert canonical_loads(expected) == value
    assert canonical_sha256(value) == hashlib.sha256(expected).hexdigest()
    assert canonical_sha256(expected) == hashlib.sha256(expected).hexdigest()


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\xef\xbb\xbf{}", "bom_forbidden"),
        (b'{"A":1,"A":1}', "duplicate_key"),
        (b'{"A":1.0}', "float_forbidden"),
        (b'{"A":1e0}', "float_forbidden"),
        (b'{"A":NaN}', "non_finite_forbidden"),
        (b'{"A":Infinity}', "non_finite_forbidden"),
        (b'{"A":9007199254740992}', "integer_out_of_range"),
        (b'{"A":"\\ud800"}', "surrogate"),
        (b'{"A":"e\\u0301"}', "non_nfc"),
        (b'{"A":"\\u00e9"}', "non_canonical"),
        (b'{"a":1,"A":2}', "non_canonical"),
        (b'{ "A":1}', "non_canonical"),
        (b'{"A":-0}', "non_canonical"),
        (b'{"_A":1}', "invalid_key"),
        (b'{"A-B":1}', "invalid_key"),
        (b'{"A":1}x', "invalid_json"),
    ],
)
def test_hcj_rejects_every_alternate_or_unsafe_spelling(raw: bytes, code: str) -> None:
    with pytest.raises(HCJError) as caught:
        canonical_loads(raw)
    assert caught.value.code == code


@pytest.mark.parametrize(
    "value",
    [
        1.0,
        (1,),
        {"A": _IntSubclass(1)},
        {_StringSubclass("A"): 1},
        bytearray(b"{}"),
    ],
)
def test_hcj_dumper_never_coerces_python_types(value: object) -> None:
    with pytest.raises(HCJError):
        canonical_dumps(value)


def test_hcj_frozen_resource_boundaries_count_root_values_and_keys() -> None:
    nested: object = None
    for _ in range(15):
        nested = [nested]
    assert canonical_dumps(nested)
    with pytest.raises(HCJError, match="depth") as caught:
        canonical_dumps([nested])
    assert caught.value.code == "too_deep"

    assert canonical_dumps({"A": None}, limits=HCJLimits(max_nodes=3))
    with pytest.raises(HCJError) as caught:
        canonical_dumps({"A": None}, limits=HCJLimits(max_nodes=2))
    assert caught.value.code == "too_many_nodes"

    members = {f"A{index:02d}": index for index in range(64)}
    assert canonical_dumps(members)
    with pytest.raises(HCJError) as caught:
        canonical_dumps({**members, "Z99": 65})
    assert caught.value.code == "object_too_large"

    assert canonical_dumps([None] * 128)
    with pytest.raises(HCJError) as caught:
        canonical_dumps([None] * 129)
    assert caught.value.code == "array_too_large"

    assert canonical_dumps({"A": "x" * 2_048})
    with pytest.raises(HCJError) as caught:
        canonical_dumps({"A": "x" * 2_049})
    assert caught.value.code == "string_too_long"

    assert canonical_dumps({"A" + "a" * 63: 1})
    with pytest.raises(HCJError) as caught:
        canonical_dumps({"A" + "a" * 64: 1})
    assert caught.value.code == "invalid_key"

    assert canonical_dumps(
        {"A": "bc"}, limits=HCJLimits(max_total_string_utf8_bytes=3)
    )
    with pytest.raises(HCJError) as caught:
        canonical_dumps(
            {"A": "bc"}, limits=HCJLimits(max_total_string_utf8_bytes=2)
        )
    assert caught.value.code == "string_budget_exceeded"

    assert canonical_dumps({"A": 1}, limits=HCJLimits(max_total_bytes=7))
    with pytest.raises(HCJError) as caught:
        canonical_dumps({"A": 1}, limits=HCJLimits(max_total_bytes=6))
    assert caught.value.code == "document_too_large"


def test_closed_wire_operation_and_route_contract_is_exact() -> None:
    expected = {
        MeshHttpOperation.PAIR_CLAIM: (
            "pair.claim",
            "POST",
            "/mesh/v1/pair/claim",
        ),
        MeshHttpOperation.PAIR_STATUS: (
            "pair.status",
            "GET",
            "/mesh/v1/pair/{pair_id}/status",
        ),
        MeshHttpOperation.PAIR_BOOTSTRAP: (
            "pair.bootstrap",
            "GET",
            "/mesh/v1/pair/{pair_id}/bootstrap",
        ),
        MeshHttpOperation.PAIR_ACK: (
            "pair.ack",
            "POST",
            "/mesh/v1/pair/{pair_id}/ack",
        ),
        MeshHttpOperation.EVENTS: (
            "event.deliver",
            "POST",
            "/mesh/v1/events",
        ),
    }
    assert set(MESH_ROUTES) == set(expected)
    for operation, (wire_value, method, path) in expected.items():
        route = MESH_ROUTES[operation]
        assert operation.value == wire_value
        assert route.method == method
        assert route.path_template == path


def test_request_known_vector_signs_exact_canonical_bytes() -> None:
    private, envelope, body = _event_request()
    expected = (
        '{"body_digest":"4e146e74e80115505c21a4fac8a0d4ee2c01b24c3f2255d33deabb731c4f9a9a",'
        '"issued_at_ms":1000,"membership_epoch":7,"method":"POST",'
        '"nonce":"nonce_1111111111111111111111111111111111111111111111111111111111111111",'
        '"op":"event.deliver","path":"/mesh/v1/events","protocol_version":1,'
        '"request_id":"req_22222222222222222222222222222222",'
        '"source_fingerprint":"hm1:146d84112d67dc9c2a6bc747ea525937de55dfe42a2b84bdc5778d40e2cc79cd",'
        '"source_public_key":"ed25519-public:v1:MOoXS6U2zGD9XWmlDMcQL4CcHIyD3DtMdt8RoVhrvFg",'
        '"space_id":"alpha",'
        '"target_fingerprint":"hm1:bf7ff449351b2e74f5c9121c25e0098f7c020d470715f8c28630e72441096c1b"}'
    ).encode("ascii")
    # The literals above are frozen interoperability vectors, not values derived
    # through the implementation under test.
    assert envelope.canonical_bytes() == expected
    assert envelope.body_digest == body_sha256(body)
    assert base64.urlsafe_b64encode(envelope.sign(private)).rstrip(b"=") == (
        b"GB13t_uIeSNKnH-4i2_ij65UjeltVghz2CzX0j1kKRRwNRtqt_w_mMCkQU0mQWQ7BHovDipdLYJgMVUZDzR-DA"
    )


def test_request_round_trip_headers_signature_and_exact_binding() -> None:
    private, envelope, body = _event_request("roundtrip")
    signature = envelope.sign(private)
    parsed, parsed_signature = MeshRequestEnvelope.from_headers(
        mesh_headers(envelope, signature)
    )

    assert parsed == envelope
    assert parsed_signature == signature
    parsed.verify(parsed_signature)
    parsed.bind_request(method="POST", path="/mesh/v1/events", body=body)

    for binding in (
        {"method": "GET", "path": envelope.path, "body": body},
        {"method": "POST", "path": "/mesh/v1/event", "body": body},
        {"method": "POST", "path": envelope.path, "body": body + b"x"},
    ):
        with pytest.raises(MeshWireError):
            parsed.bind_request(**binding)  # type: ignore[arg-type]


def test_every_still_well_formed_request_field_is_covered_by_signature() -> None:
    private, envelope, _ = _event_request("signed-fields")
    signature = envelope.sign(private)
    _, other_public, other_fingerprint = _identity("other-source")
    _, _, other_target = _identity("other-target")
    mutations: list[dict[str, object]] = [
        {"space_id": "beta"},
        {
            "source_public_key": other_public,
            "source_fingerprint": other_fingerprint,
        },
        {"target_fingerprint": other_target},
        {"membership_epoch": 8},
        {"request_id": "req_" + "3" * 32},
        {"nonce": "nonce_" + "4" * 64},
        {"issued_at_ms": 1_001},
        {"body_digest": "5" * 64},
    ]
    for changes in mutations:
        value = envelope.as_dict()
        value.update(changes)
        changed = MeshRequestEnvelope.from_bytes(canonical_dumps(value))
        with pytest.raises(MeshWireError) as caught:
            changed.verify(signature)
        assert caught.value.code == "authentication_failed"


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol_version": 2},
        {"op": "events"},
        {"method": "GET"},
        {"path": "/mesh/v1/events/"},
        {"space_id": "alpha/beta"},
        {"source_public_key": "ed25519:" + "A" * 43},
        {"source_fingerprint": "hm1:" + "0" * 64},
        {"target_fingerprint": "mesh-target-unbound-v1"},
        {"membership_epoch": -1},
        {"request_id": "pair_" + "1" * 32},
        {"nonce": "nonce_" + "A" * 64},
        {"body_digest": "A" * 64},
    ],
)
def test_request_parser_rejects_invalid_or_legacy_wire_fields(
    changes: dict[str, object],
) -> None:
    _, envelope, _ = _event_request("invalid-fields")
    value = envelope.as_dict()
    value.update(changes)
    with pytest.raises(MeshWireError):
        MeshRequestEnvelope.from_bytes(canonical_dumps(value))


def test_request_shape_is_closed_and_pair_path_binds_pair_id() -> None:
    _, envelope, _ = _event_request("closed-shape")
    value = envelope.as_dict()
    value["unknown"] = 1
    with pytest.raises(MeshWireError) as caught:
        MeshRequestEnvelope.from_bytes(canonical_dumps(value))
    assert caught.value.code == "invalid_envelope_shape"

    pair = _pair_request(MeshHttpOperation.PAIR_STATUS)
    changed = pair.as_dict()
    changed["path"] = "/mesh/v1/pair/pair_" + "b" * 32 + "/status"
    with pytest.raises(MeshWireError) as caught:
        MeshRequestEnvelope.from_bytes(canonical_dumps(changed))
    assert caught.value.code == "pair_id_mismatch"


def test_get_routes_are_intrinsically_zero_body() -> None:
    pair = _pair_request(MeshHttpOperation.PAIR_BOOTSTRAP)
    assert pair.body_digest == hashlib.sha256(b"").hexdigest()
    pair.bind_request(method="GET", path=pair.path, body=b"")
    with pytest.raises(MeshWireError) as caught:
        pair.bind_request(method="GET", path=pair.path, body=b"x")
    assert caught.value.code == "get_body_forbidden"

    private, public, fingerprint = _identity("nonempty-get")
    del private
    _, _, target = _identity("nonempty-get-target")
    with pytest.raises(MeshWireError) as caught:
        MeshRequestEnvelope.create(
            op=MeshHttpOperation.PAIR_STATUS,
            path="/mesh/v1/pair/pair_" + "a" * 32 + "/status",
            space_id="alpha",
            source_public_key=public,
            source_fingerprint=fingerprint,
            target_fingerprint=target,
            membership_epoch=1,
            request_id="pair_" + "a" * 32,
            nonce="nonce_" + "b" * 64,
            issued_at_ms=1,
            body=b"x",
        )
    assert caught.value.code == "get_body_forbidden"


def test_mesh_headers_are_exact_singletons_and_canonical_base64url() -> None:
    private, envelope, _ = _event_request("headers")
    signature = envelope.sign(private)
    headers = mesh_headers(envelope, signature)
    encoded_envelope = headers[0][1]
    encoded_signature = headers[1][1]
    assert headers[0][0] == MESH_ENVELOPE_HEADER
    assert headers[1][0] == MESH_SIGNATURE_HEADER
    assert b"=" not in encoded_envelope + encoded_signature
    assert len(encoded_signature) == 86

    upper = (
        (b"Hivemind-Mesh-Envelope", encoded_envelope),
        (b"HIVEMIND-MESH-SIGNATURE", encoded_signature),
    )
    assert parse_mesh_headers(upper) == (envelope.canonical_bytes(), signature)

    invalid = [
        headers + (headers[0],),
        headers + ((b"Hivemind-Mesh-Signature", encoded_signature),),
        ((MESH_ENVELOPE_HEADER, encoded_envelope + b"="), headers[1]),
        ((MESH_ENVELOPE_HEADER, encoded_envelope + b","), headers[1]),
        ((MESH_ENVELOPE_HEADER, encoded_envelope + b"\r\n A"), headers[1]),
        ((MESH_ENVELOPE_HEADER, b"AB"), headers[1]),
        headers + ((b"hivemind-mesh-extra", b"A"),),
    ]
    for candidate in invalid:
        with pytest.raises(MeshWireError):
            parse_mesh_headers(candidate)


def test_mesh_header_encoded_and_decoded_caps_are_exact() -> None:
    signature = base64.urlsafe_b64encode(b"\0" * 64).rstrip(b"=")
    at_limit = b"A" * MAX_ENVELOPE_HEADER_BYTES
    decoded, parsed_signature = parse_mesh_headers(
        (
            (MESH_ENVELOPE_HEADER, at_limit),
            (MESH_SIGNATURE_HEADER, signature),
        )
    )
    assert len(decoded) == MAX_ENVELOPE_DECODED_BYTES
    assert parsed_signature == b"\0" * 64

    with pytest.raises(MeshWireError):
        parse_mesh_headers(
            (
                (MESH_ENVELOPE_HEADER, at_limit + b"A"),
                (MESH_SIGNATURE_HEADER, signature),
            )
        )


def test_response_matrix_is_closed_signed_bound_and_acknowledges_only_success() -> None:
    from live_mem.mesh.wire import MESH_SUCCESS_CODES

    private, public, fingerprint = _identity("response-local")
    _, _, target = _identity("response-target")
    for code, status in MESH_RESPONSE_STATUS.items():
        body = canonical_dumps({"code": code.value})
        envelope = MeshResponseEnvelope.create(
            code=code,
            correlation_id="req_" + "c" * 32,
            source_public_key=public,
            source_fingerprint=fingerprint,
            target_fingerprint=target,
            issued_at_ms=2_000,
            nonce="nonce_" + "d" * 64,
            body=body,
        )
        if code in MESH_SUCCESS_CODES:
            # P10-3 success codes acknowledge and carry a data body.
            assert status in {200, 202}
            assert envelope.acknowledged is True
        else:
            # Every P10-2 refusal stays byte-shape compatible: never acknowledged.
            assert status in {400, 403, 409, 423, 503}
            assert status >= 400
            assert envelope.acknowledged is False
        assert envelope.status == status
        signature = envelope.sign(private)
        parsed, parsed_signature = MeshResponseEnvelope.from_headers(
            mesh_headers(envelope, signature)
        )
        parsed.verify(parsed_signature)
        parsed.bind_response(status=status, body=body)
        with pytest.raises(MeshWireError):
            parsed.bind_response(status=status, body=body + b"x")

        with pytest.raises(InvalidSignature):
            parse_mesh_public_key(public).verify(
                signature,
                MESH_REQUEST_SIGNATURE_DOMAIN + envelope.canonical_bytes(),
            )

    assert set(MESH_RESPONSE_STATUS) == set(MeshResponseCode)
    assert MESH_REQUEST_SIGNATURE_DOMAIN != MESH_RESPONSE_SIGNATURE_DOMAIN


class FakeReplayStorage:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.put_calls = 0
        self.get_calls = 0
        self.delete_calls = 0
        self.list_calls = 0
        self.last_list_max_keys: int | None = None
        self.block_put = False
        self.block_readback = False
        self.tamper_readback = False
        self.put_started = asyncio.Event()
        self.put_release = asyncio.Event()
        self.readback_started = asyncio.Event()
        self.readback_release = asyncio.Event()
        self._put_completed_keys: set[str] = set()

    @property
    def io_calls(self) -> int:
        return self.put_calls + self.get_calls + self.delete_calls + self.list_calls

    async def put(
        self,
        key: str,
        content: str,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        assert content_type == "application/json"
        self.put_calls += 1
        if self.block_put:
            self.put_started.set()
            await self.put_release.wait()
        self.objects[key] = content
        self._put_completed_keys.add(key)

    async def get(self, key: str) -> str | None:
        self.get_calls += 1
        if self.block_readback and key in self._put_completed_keys:
            self.readback_started.set()
            await self.readback_release.wait()
        value = self.objects.get(key)
        if self.tamper_readback and key in self._put_completed_keys and value is not None:
            return value + " "
        return value

    async def delete(self, key: str) -> None:
        self.delete_calls += 1
        self.objects.pop(key, None)
        self._put_completed_keys.discard(key)

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        self.list_calls += 1
        self.last_list_max_keys = max_keys
        keys = [key for key in sorted(self.objects) if key.startswith(prefix)]
        if max_keys:
            keys = keys[:max_keys]
        return [{"Key": key, "Size": len(self.objects[key])} for key in keys]


def test_replay_ttl_is_the_single_wire_freshness_contract() -> None:
    assert MESH_REQUEST_FRESHNESS_WINDOW_MS == 300_000
    assert TRANSPORT_REPLAY_TTL_MS == MESH_REQUEST_FRESHNESS_WINDOW_MS
    assert FRESHNESS_WINDOW_MS == MESH_REQUEST_FRESHNESS_WINDOW_MS


@pytest.mark.asyncio
@pytest.mark.parametrize("delta", [-1, 1])
async def test_replay_admission_rejects_nonexact_expiry_before_io(delta: int) -> None:
    storage = FakeReplayStorage()
    capability = object()
    ledger = DurableReplayLedger(
        storage,
        prefix=_prefix(f"invalid-expiry-{delta}"),
        authority_capability=capability,
    )
    _, envelope, _ = _event_request(f"invalid-expiry-source-{delta}")
    with pytest.raises(ReplayError) as caught:
        await ledger.admit_verified(
            envelope,
            authority_capability=capability,
            now_ms=envelope.issued_at_ms,
            expires_at_ms=_replay_expiry(envelope) + delta,
        )
    assert caught.value.code == "invalid_replay"
    assert storage.io_calls == 0
    assert ledger._key_locks == {}
    assert ledger._key_lock_users == {}


@pytest.mark.asyncio
async def test_replay_capability_and_pair_refusal_happen_before_any_io() -> None:
    storage = FakeReplayStorage()
    capability = object()
    ledger = DurableReplayLedger(
        storage,
        prefix=_prefix("authority"),
        authority_capability=capability,
    )
    _, event, _ = _event_request("authority-source")

    with pytest.raises(ReplayError) as caught:
        await ledger.admit_verified(
            event,
            authority_capability=object(),
            now_ms=1_000,
            expires_at_ms=1_100,
        )
    assert caught.value.code == "authority_required"
    assert storage.io_calls == 0

    with pytest.raises(ReplayError) as caught:
        await ledger.admit_verified(
            _pair_request(),
            authority_capability=capability,
            now_ms=1_000,
            expires_at_ms=1_100,
        )
    assert caught.value.code == "pair_replay_forbidden"
    assert storage.io_calls == 0


@pytest.mark.asyncio
async def test_replay_first_write_duplicate_conflict_restart_and_minimal_record() -> None:
    storage = FakeReplayStorage()
    capability = object()
    prefix = _prefix("restart")
    ledger = DurableReplayLedger(
        storage, prefix=prefix, authority_capability=capability
    )
    _, envelope, body = _event_request("restart-source")

    assert (
        await ledger.admit_verified(
            envelope,
            authority_capability=capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(envelope),
        )
        is ReplayDecision.ADMITTED
    )
    assert storage.put_calls == 1
    assert ledger._key_locks == {}
    assert ledger._key_lock_users == {}
    stored = next(iter(storage.objects.values()))
    stored_value = canonical_loads(stored.encode("utf-8"))
    assert type(stored_value) is dict
    assert stored_value["expires_at_ms"] == _replay_expiry(envelope)
    assert envelope.source_public_key not in stored
    assert body.decode("ascii") not in stored
    assert "endpoint" not in stored
    assert "private" not in stored

    assert (
        await ledger.admit_verified(
            envelope,
            authority_capability=capability,
            now_ms=1_001,
            expires_at_ms=_replay_expiry(envelope),
        )
        is ReplayDecision.DUPLICATE
    )
    assert storage.put_calls == 1

    _, conflict, _ = _event_request(
        "restart-source", nonce_hex="1", request_hex="3"
    )
    with pytest.raises(ReplayError) as caught:
        await ledger.admit_verified(
            conflict,
            authority_capability=capability,
            now_ms=1_002,
            expires_at_ms=_replay_expiry(conflict),
        )
    assert caught.value.code == "replay_conflict"
    assert storage.put_calls == 1

    restarted_capability = object()
    restarted = DurableReplayLedger(
        storage,
        prefix=prefix,
        authority_capability=restarted_capability,
    )
    assert (
        await restarted.admit_verified(
            envelope,
            authority_capability=restarted_capability,
            now_ms=1_003,
            expires_at_ms=_replay_expiry(envelope),
        )
        is ReplayDecision.DUPLICATE
    )
    assert storage.put_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["shortened", "lengthened", "noncanonical"])
async def test_restart_rejects_mutated_expiry_before_gc_or_readmission(
    mutation: str,
) -> None:
    storage = FakeReplayStorage()
    capability = object()
    prefix = _prefix("expiry-mutation-" + mutation)
    ledger = DurableReplayLedger(
        storage, prefix=prefix, authority_capability=capability
    )
    _, envelope, _ = _event_request("expiry-mutation-source-" + mutation)
    exact_expiry = _replay_expiry(envelope)
    await ledger.admit_verified(
        envelope,
        authority_capability=capability,
        now_ms=envelope.issued_at_ms,
        expires_at_ms=exact_expiry,
    )
    key = next(iter(storage.objects))
    raw = storage.objects[key]
    if mutation == "noncanonical":
        needle = f'"expires_at_ms":{exact_expiry}'
        assert needle in raw
        raw = raw.replace(needle, '"expires_at_ms":3.01e5')
    else:
        value = canonical_loads(raw.encode("utf-8"))
        assert type(value) is dict
        value["expires_at_ms"] = exact_expiry + (
            -1 if mutation == "shortened" else 1
        )
        raw = canonical_dumps(value).decode("utf-8")
    storage.objects[key] = raw
    snapshot = dict(storage.objects)
    delete_calls = storage.delete_calls
    put_calls = storage.put_calls

    restarted_capability = object()
    restarted = DurableReplayLedger(
        storage,
        prefix=prefix,
        authority_capability=restarted_capability,
    )
    with pytest.raises(ReplayError) as caught:
        await restarted.admit_verified(
            envelope,
            authority_capability=restarted_capability,
            # At the exact canonical boundary a shortened record would have
            # been GC'd and the same nonce re-admitted by the vulnerable path.
            now_ms=exact_expiry,
            expires_at_ms=exact_expiry,
        )
    assert caught.value.code == "local_unsafe"
    assert restarted.unsafe
    assert storage.delete_calls == delete_calls
    assert storage.put_calls == put_calls
    assert storage.objects == snapshot


@pytest.mark.asyncio
async def test_candidate_nonce_is_compared_before_gc_for_coherent_time_shift() -> None:
    storage = FakeReplayStorage()
    capability = object()
    prefix = _prefix("coherent-expiry-shift")
    ledger = DurableReplayLedger(
        storage, prefix=prefix, authority_capability=capability
    )
    _, envelope, _ = _event_request("coherent-expiry-shift-source")
    exact_expiry = _replay_expiry(envelope)
    await ledger.admit_verified(
        envelope,
        authority_capability=capability,
        now_ms=envelope.issued_at_ms,
        expires_at_ms=exact_expiry,
    )
    key = next(iter(storage.objects))
    value = canonical_loads(storage.objects[key].encode("utf-8"))
    assert type(value) is dict
    value["issued_at_ms"] = 0
    value["expires_at_ms"] = TRANSPORT_REPLAY_TTL_MS
    storage.objects[key] = canonical_dumps(value).decode("utf-8")
    snapshot = dict(storage.objects)

    restarted_capability = object()
    restarted = DurableReplayLedger(
        storage,
        prefix=prefix,
        authority_capability=restarted_capability,
    )
    with pytest.raises(ReplayError) as caught:
        await restarted.admit_verified(
            envelope,
            authority_capability=restarted_capability,
            # The altered record is strictly expired here.  Candidate-first
            # comparison must reject it before generic GC can delete the key.
            now_ms=exact_expiry,
            expires_at_ms=exact_expiry,
        )
    assert caught.value.code == "replay_conflict"
    assert not restarted.unsafe
    assert storage.delete_calls == 0
    assert storage.put_calls == 1
    assert storage.objects == snapshot


@pytest.mark.asyncio
async def test_replay_key_known_vector_includes_request_signature_domain() -> None:
    storage = FakeReplayStorage()
    capability = object()
    prefix = _prefix("key-vector")
    ledger = DurableReplayLedger(
        storage, prefix=prefix, authority_capability=capability
    )
    _, envelope, _ = _event_request()
    expected_digest = hashlib.sha256(
        REPLAY_KEY_DOMAIN
        + MESH_REQUEST_SIGNATURE_DOMAIN
        + envelope.source_fingerprint.encode("ascii")
        + b"\0"
        + envelope.nonce.encode("ascii")
    ).hexdigest()
    assert expected_digest == (
        "cdfeeb7eb9cc2a7c29e996a8a6081aa1a305220a0d4191a3396499c2e9ea8971"
    )

    await ledger.admit_verified(
        envelope,
        authority_capability=capability,
        now_ms=1_000,
        expires_at_ms=_replay_expiry(envelope),
    )
    assert set(storage.objects) == {prefix + expected_digest + ".json"}


@pytest.mark.asyncio
async def test_concurrent_identical_replay_has_exactly_one_durable_write() -> None:
    storage = FakeReplayStorage()
    capability = object()
    ledger = DurableReplayLedger(
        storage,
        prefix=_prefix("concurrency"),
        authority_capability=capability,
    )
    _, envelope, _ = _event_request("concurrency-source")

    results = await asyncio.gather(
        *(
            ledger.admit_verified(
                envelope,
                authority_capability=capability,
                now_ms=1_000,
                expires_at_ms=_replay_expiry(envelope),
            )
            for _ in range(20)
        )
    )
    assert results.count(ReplayDecision.ADMITTED) == 1
    assert results.count(ReplayDecision.DUPLICATE) == 19
    assert storage.put_calls == 1
    assert len(storage.objects) == 1
    assert ledger._key_locks == {}
    assert ledger._key_lock_users == {}


@pytest.mark.asyncio
async def test_replay_restart_overflow_load_is_bounded_and_poisoned() -> None:
    storage = FakeReplayStorage()
    prefix = _prefix("overflow-load")
    producer_capability = object()
    producer = DurableReplayLedger(
        storage,
        prefix=prefix,
        authority_capability=producer_capability,
        global_limit=3,
        per_signer_limit=3,
    )
    envelopes: list[MeshRequestEnvelope] = []
    for digit in ("1", "2", "3"):
        _, envelope, _ = _event_request(
            "overflow-source", nonce_hex=digit, request_hex=digit
        )
        envelopes.append(envelope)
        await producer.admit_verified(
            envelope,
            authority_capability=producer_capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(envelope),
        )
    assert len(storage.objects) == 3

    restart_capability = object()
    restarted = DurableReplayLedger(
        storage,
        prefix=prefix,
        authority_capability=restart_capability,
        global_limit=2,
        per_signer_limit=2,
    )
    with pytest.raises(ReplayError) as caught:
        await restarted.admit_verified(
            envelopes[0],
            authority_capability=restart_capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(envelopes[0]),
        )
    assert caught.value.code == "local_unsafe"
    assert restarted.unsafe
    assert storage.last_list_max_keys == 3


@pytest.mark.asyncio
async def test_replay_global_and_per_signer_quota_never_evict_fresh_records() -> None:
    global_storage = FakeReplayStorage()
    global_capability = object()
    global_ledger = DurableReplayLedger(
        global_storage,
        prefix=_prefix("global-quota"),
        authority_capability=global_capability,
        global_limit=2,
        per_signer_limit=2,
    )
    for digit in ("1", "2"):
        _, envelope, _ = _event_request(
            "global-source", nonce_hex=digit, request_hex=digit
        )
        assert (
            await global_ledger.admit_verified(
                envelope,
                authority_capability=global_capability,
                now_ms=1_000,
                expires_at_ms=_replay_expiry(envelope),
            )
            is ReplayDecision.ADMITTED
        )
    snapshot = dict(global_storage.objects)
    _, third, _ = _event_request(
        "global-source", nonce_hex="3", request_hex="3"
    )
    with pytest.raises(ReplayError) as caught:
        await global_ledger.admit_verified(
            third,
            authority_capability=global_capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(third),
        )
    assert caught.value.code == "replay_saturated"
    assert global_storage.objects == snapshot

    signer_storage = FakeReplayStorage()
    signer_capability = object()
    signer_ledger = DurableReplayLedger(
        signer_storage,
        prefix=_prefix("signer-quota"),
        authority_capability=signer_capability,
        global_limit=3,
        per_signer_limit=1,
    )
    _, first, _ = _event_request("one-signer", nonce_hex="4", request_hex="4")
    assert (
        await signer_ledger.admit_verified(
            first,
            authority_capability=signer_capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(first),
        )
        is ReplayDecision.ADMITTED
    )
    _, second_same_signer, _ = _event_request(
        "one-signer", nonce_hex="5", request_hex="5"
    )
    with pytest.raises(ReplayError) as caught:
        await signer_ledger.admit_verified(
            second_same_signer,
            authority_capability=signer_capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(second_same_signer),
        )
    assert caught.value.code == "replay_saturated"
    _, other_signer, _ = _event_request(
        "other-signer", nonce_hex="6", request_hex="6"
    )
    assert (
        await signer_ledger.admit_verified(
            other_signer,
            authority_capability=signer_capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(other_signer),
        )
        is ReplayDecision.ADMITTED
    )


@pytest.mark.asyncio
async def test_replay_gc_uses_strict_expiry_boundary() -> None:
    storage = FakeReplayStorage()
    capability = object()
    ledger = DurableReplayLedger(
        storage,
        prefix=_prefix("strict-expiry"),
        authority_capability=capability,
        global_limit=1,
        per_signer_limit=1,
    )
    _, first, _ = _event_request(
        "expiry-source", nonce_hex="7", request_hex="7", issued_at_ms=100
    )
    first_expiry = _replay_expiry(first)
    await ledger.admit_verified(
        first,
        authority_capability=capability,
        now_ms=100,
        expires_at_ms=first_expiry,
    )
    restarted_capability = object()
    restarted = DurableReplayLedger(
        storage,
        prefix=_prefix("strict-expiry"),
        authority_capability=restarted_capability,
        global_limit=1,
        per_signer_limit=1,
    )
    _, second, _ = _event_request(
        "expiry-source",
        nonce_hex="8",
        request_hex="8",
        issued_at_ms=first_expiry,
    )
    with pytest.raises(ReplayError) as caught:
        await restarted.admit_verified(
            second,
            authority_capability=restarted_capability,
            now_ms=first_expiry,
            expires_at_ms=_replay_expiry(second),
        )
    assert caught.value.code == "replay_saturated"
    assert storage.delete_calls == 0

    assert (
        await restarted.admit_verified(
            second,
            authority_capability=restarted_capability,
            now_ms=first_expiry + 1,
            expires_at_ms=_replay_expiry(second),
        )
        is ReplayDecision.ADMITTED
    )
    assert storage.delete_calls == 1
    assert len(storage.objects) == 1


@pytest.mark.asyncio
async def test_corrupt_or_nonidentical_replay_state_permanently_poisons_prefix() -> None:
    corrupt_storage = FakeReplayStorage()
    prefix = _prefix("corrupt")
    corrupt_storage.objects[prefix + "0" * 64 + ".json"] = "{}"
    capability = object()
    ledger = DurableReplayLedger(
        corrupt_storage, prefix=prefix, authority_capability=capability
    )
    _, envelope, _ = _event_request("corrupt-source")
    with pytest.raises(ReplayError) as caught:
        await ledger.admit_verified(
            envelope,
            authority_capability=capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(envelope),
        )
    assert caught.value.code == "local_unsafe"
    assert ledger.unsafe

    calls = corrupt_storage.io_calls
    restarted_capability = object()
    restarted = DurableReplayLedger(
        corrupt_storage,
        prefix=prefix,
        authority_capability=restarted_capability,
    )
    with pytest.raises(ReplayError) as caught:
        await restarted.admit_verified(
            envelope,
            authority_capability=restarted_capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(envelope),
        )
    assert caught.value.code == "local_unsafe"
    assert corrupt_storage.io_calls == calls

    mismatch_storage = FakeReplayStorage()
    mismatch_storage.tamper_readback = True
    mismatch_capability = object()
    mismatch = DurableReplayLedger(
        mismatch_storage,
        prefix=_prefix("readback-mismatch"),
        authority_capability=mismatch_capability,
    )
    with pytest.raises(ReplayError) as caught:
        await mismatch.admit_verified(
            envelope,
            authority_capability=mismatch_capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(envelope),
        )
    assert caught.value.code == "local_unsafe"
    assert mismatch.unsafe


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["put", "readback"])
async def test_replay_cancellation_waits_for_ambiguous_io_to_settle(stage: str) -> None:
    storage = FakeReplayStorage()
    storage.block_put = stage == "put"
    storage.block_readback = stage == "readback"
    capability = object()
    ledger = DurableReplayLedger(
        storage,
        prefix=_prefix("cancel-" + stage),
        authority_capability=capability,
    )
    _, envelope, _ = _event_request("cancel-source-" + stage)
    task = asyncio.create_task(
        ledger.admit_verified(
            envelope,
            authority_capability=capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(envelope),
        )
    )
    started = storage.put_started if stage == "put" else storage.readback_started
    release = storage.put_release if stage == "put" else storage.readback_release
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not ledger.unsafe
    assert len(storage.objects) == 1
    assert (
        await ledger.admit_verified(
            envelope,
            authority_capability=capability,
            now_ms=1_001,
            expires_at_ms=_replay_expiry(envelope),
        )
        is ReplayDecision.DUPLICATE
    )


@pytest.mark.asyncio
async def test_replay_timeout_settles_success_then_retry_is_duplicate() -> None:
    storage = FakeReplayStorage()
    storage.block_put = True
    capability = object()
    ledger = DurableReplayLedger(
        storage,
        prefix=_prefix("timeout"),
        authority_capability=capability,
        io_timeout_seconds=0.001,
    )
    _, envelope, _ = _event_request("timeout-source")
    task = asyncio.create_task(
        ledger.admit_verified(
            envelope,
            authority_capability=capability,
            now_ms=1_000,
            expires_at_ms=_replay_expiry(envelope),
        )
    )
    await asyncio.wait_for(storage.put_started.wait(), timeout=1)
    await asyncio.sleep(0.01)
    assert not task.done()
    storage.put_release.set()
    with pytest.raises(ReplayError) as caught:
        await task
    assert caught.value.code == "storage_timeout"
    assert not ledger.unsafe
    assert (
        await ledger.admit_verified(
            envelope,
            authority_capability=capability,
            now_ms=1_001,
            expires_at_ms=_replay_expiry(envelope),
        )
        is ReplayDecision.DUPLICATE
    )


@pytest.fixture
def supported_process_lock_filesystem(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[int, int]]:
    calls: list[tuple[int, int]] = []

    def accept(fd: int) -> None:
        info = os.fstat(fd)
        calls.append((info.st_dev, info.st_ino))

    monkeypatch.setattr(mesh_replay, "require_supported_filesystem", accept)
    return calls


def _assert_process_lock_filesystem_guard_source(source: str) -> None:
    parent_guard = source.index("_require_process_lock_filesystem(parent_fd)")
    directory_creation = source.index("os.mkdir(", parent_guard)
    directory_guard = source.index(
        "_require_process_lock_filesystem(directory_fd)", directory_creation
    )
    file_creation = source.index(
        "flags | os.O_CREAT | os.O_EXCL", directory_guard
    )
    file_guard = source.index("_require_process_lock_filesystem(fd)", file_creation)
    file_mutation = source.index("os.fchmod(fd, 0o600)", file_guard)
    assert (
        parent_guard
        < directory_creation
        < directory_guard
        < file_creation
        < file_guard
        < file_mutation
    )


def test_process_identity_lock_rejects_unsupported_filesystem_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.chmod(tmp_path, 0o700)
    calls = 0

    def reject(_fd: int) -> None:
        nonlocal calls
        calls += 1
        raise SecretFilesystemError("fixture NFS detail must not be reflected")

    monkeypatch.setattr(mesh_replay, "require_supported_filesystem", reject)
    _, _, fingerprint = _identity("process-lock-network-filesystem")
    lock_directory = tmp_path / "unsupported"
    with pytest.raises(ReplayError) as caught:
        MeshProcessIdentityLock(lock_directory, fingerprint).acquire(
            timeout_seconds=0
        )
    assert caught.value.code == "process_lock_unsafe"
    assert caught.value.safe_message == "Mesh process lock is unsafe"
    assert "NFS" not in str(caught.value)
    assert calls == 1
    assert not lock_directory.exists()


def test_process_identity_lock_checks_filesystem_for_every_bound_state(
    tmp_path: Path,
    supported_process_lock_filesystem: list[tuple[int, int]],
) -> None:
    os.chmod(tmp_path, 0o700)
    _, _, fingerprint = _identity("process-lock-filesystem-guards")
    lock_directory = tmp_path / "guarded"
    lock = MeshProcessIdentityLock(lock_directory, fingerprint)
    lock.acquire(timeout_seconds=0)
    lock.close()

    entries = list(lock_directory.iterdir())
    assert len(entries) == 1
    expected = [
        (tmp_path.stat().st_dev, tmp_path.stat().st_ino),
        (lock_directory.stat().st_dev, lock_directory.stat().st_ino),
        (entries[0].stat().st_dev, entries[0].stat().st_ino),
    ]
    assert supported_process_lock_filesystem == expected


@pytest.mark.parametrize(
    "guard",
    [
        "_require_process_lock_filesystem(parent_fd)",
        "_require_process_lock_filesystem(directory_fd)",
        "_require_process_lock_filesystem(fd)",
    ],
)
def test_mutation_red_process_identity_lock_filesystem_guard_removed(
    guard: str,
) -> None:
    source = inspect.getsource(MeshProcessIdentityLock.acquire)
    _assert_process_lock_filesystem_guard_source(source)
    mutant = source.replace(guard, "None", 1)
    with pytest.raises(ValueError):
        _assert_process_lock_filesystem_guard_source(mutant)


def test_process_identity_lock_is_hashed_nofollow_0600_single_link_and_retained(
    tmp_path: Path,
    supported_process_lock_filesystem: list[tuple[int, int]],
) -> None:
    del supported_process_lock_filesystem
    os.chmod(tmp_path, 0o700)
    _, _, fingerprint = _identity("process-lock")
    lock = MeshProcessIdentityLock(tmp_path, fingerprint)
    lock.acquire(timeout_seconds=0)
    try:
        files = list(tmp_path.iterdir())
        assert len(files) == 1
        path = files[0]
        expected_digest = hashlib.sha256(
            PROCESS_LOCK_DOMAIN + fingerprint.encode("ascii")
        ).hexdigest()
        assert path.name == expected_digest + ".lock"
        info = path.stat()
        assert stat.S_ISREG(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_nlink == 1
        assert path.read_text(encoding="ascii") == expected_digest + "\n"

        second = MeshProcessIdentityLock(tmp_path, fingerprint)
        with pytest.raises(ReplayError) as caught:
            second.acquire(timeout_seconds=0)
        assert caught.value.code == "process_lock_timeout"
    finally:
        lock.close()
    assert path.exists()

    second.acquire(timeout_seconds=0)
    second.close()

    symlink_dir = tmp_path / "symlink"
    symlink_dir.mkdir()
    os.chmod(symlink_dir, 0o700)
    target = symlink_dir / "target"
    target.write_text("untrusted", encoding="ascii")
    os.chmod(target, 0o600)
    (symlink_dir / (expected_digest + ".lock")).symlink_to(target)
    with pytest.raises(ReplayError) as caught:
        MeshProcessIdentityLock(symlink_dir, fingerprint).acquire(timeout_seconds=0)
    assert caught.value.code == "process_lock_failed"


def test_process_identity_lock_refuses_inherited_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    supported_process_lock_filesystem: list[tuple[int, int]],
) -> None:
    del supported_process_lock_filesystem
    os.chmod(tmp_path, 0o700)
    lock_directory = tmp_path / "owned"
    _, _, fingerprint = _identity("process-lock-fork")
    lock = MeshProcessIdentityLock(lock_directory, fingerprint)
    lock.acquire(timeout_seconds=0)
    owner_pid = os.getpid()
    assert lock.acquired

    monkeypatch.setattr(os, "getpid", lambda: owner_pid + 1)
    assert not lock.acquired
    with pytest.raises(ReplayError) as caught:
        lock.acquire(timeout_seconds=0)
    assert caught.value.code == "process_lock_inherited"
    lock.close()
    assert not lock.acquired


def test_process_identity_lock_rejects_unsafe_directory_and_file_modes(
    tmp_path: Path,
    supported_process_lock_filesystem: list[tuple[int, int]],
) -> None:
    del supported_process_lock_filesystem
    os.chmod(tmp_path, 0o700)
    lock_directory = tmp_path / "unsafe-mode"
    lock_directory.mkdir(mode=0o700)
    os.chmod(lock_directory, 0o755)
    _, _, fingerprint = _identity("process-lock-modes")
    lock = MeshProcessIdentityLock(lock_directory, fingerprint)
    with pytest.raises(ReplayError) as caught:
        lock.acquire(timeout_seconds=0)
    assert caught.value.code == "process_lock_unsafe"

    os.chmod(lock_directory, 0o700)
    digest = hashlib.sha256(
        PROCESS_LOCK_DOMAIN + fingerprint.encode("ascii")
    ).hexdigest()
    lock_path = lock_directory / (digest + ".lock")
    lock_path.write_text(digest + "\n", encoding="ascii")
    os.chmod(lock_path, 0o644)
    with pytest.raises(ReplayError) as caught:
        lock.acquire(timeout_seconds=0)
    assert caught.value.code == "process_lock_unsafe"
