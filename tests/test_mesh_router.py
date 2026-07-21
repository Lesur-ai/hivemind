"""ASGI refusal-order and no-business-mutation proof for P10-2 Mesh routes."""

from __future__ import annotations

import base64
from typing import Any

import pytest

from live_mem.auth.middleware import AuthMiddleware
from live_mem.core.hivemind import layout
from live_mem.core.hivemind.models import (
    EventEnvelope,
    EventType,
    HiveNodeStatus,
    Member,
    MembershipView,
    NodeHealth,
    NodeIdentity,
)
from live_mem.mesh.canonical import canonical_dumps
from live_mem.mesh.config import (
    MESH_BOOTSTRAP_MAX_BYTES_ENV,
    MESH_BOOTSTRAP_MAX_OBJECTS_ENV,
    MESH_CONTROL_MAX_BYTES_ENV,
    MESH_DISPLAY_NAME_ENV,
    MESH_ENABLED_ENV,
    MESH_INVITATION_TTL_ENV,
    MESH_PRIVATE_KEY_ENV,
    MESH_PUBLIC_URL_ENV,
    load_mesh_config,
)
from live_mem.mesh.identity import (
    MESH_PRIVATE_KEY_PREFIX,
    MeshPrivateKey,
    mesh_identity_fingerprint,
    parse_mesh_private_key,
)
from live_mem.mesh.router import MeshNamespaceRouter
from live_mem.mesh.wire import (
    MESH_ROUTES,
    MeshHttpOperation,
    MeshRequestEnvelope,
    MeshResponseCode,
    MeshResponseEnvelope,
    mesh_headers,
)


NOW_MS = 1_800_000_000_000
REQUEST_ID = "req_" + "1" * 32
PAIR_ID = "pair_" + "2" * 32
NONCE = "nonce_" + "3" * 64


def _private(seed: int) -> MeshPrivateKey:
    raw = bytes([seed]) * 32
    encoded = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(raw).decode(
        "ascii"
    ).rstrip("=")
    return parse_mesh_private_key(encoded)


def _encoded_private(seed: int) -> str:
    raw = bytes([seed]) * 32
    return MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(raw).decode(
        "ascii"
    ).rstrip("=")


def _config(*, control_max_bytes: int = 262_144):
    config = load_mesh_config(
        {
            MESH_ENABLED_ENV: "true",
            MESH_PUBLIC_URL_ENV: "https://local.example.test",
            MESH_PRIVATE_KEY_ENV: _encoded_private(7),
            MESH_DISPLAY_NAME_ENV: "Local peer",
            MESH_INVITATION_TTL_ENV: "3600",
            MESH_CONTROL_MAX_BYTES_ENV: str(control_max_bytes),
            MESH_BOOTSTRAP_MAX_BYTES_ENV: "268435456",
            MESH_BOOTSTRAP_MAX_OBJECTS_ENV: "50000",
        }
    )
    assert config is not None
    return config


class FakeStorage:
    def __init__(self, objects: dict[str, str] | None = None) -> None:
        self.objects = dict(objects or {})
        self.reads: list[str] = []
        self.writes: list[tuple[str, Any]] = []

    async def get(self, key: str) -> str | None:
        self.reads.append(key)
        return self.objects.get(key)

    async def put(
        self,
        key: str,
        value: Any,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        del content_type
        self.writes.append((key, value))
        self.objects[key] = value

    async def delete(self, key: str) -> None:
        self.writes.append((key, None))
        self.objects.pop(key, None)

    async def list_objects(
        self, prefix: str, max_keys: int = 0
    ) -> list[dict[str, Any]]:
        matches = [
            {"Key": key, "Size": len(value.encode("utf-8"))}
            for key, value in sorted(self.objects.items())
            if key.startswith(prefix)
        ]
        return matches[:max_keys] if max_keys > 0 else matches


class FakeReplayLedger:
    def __init__(
        self,
        decision: Any = None,
        error: BaseException | None = None,
        unsafe: bool = False,
    ) -> None:
        self.decision = decision
        self.error = error
        self.unsafe = unsafe
        self.calls: list[dict[str, Any]] = []

    def assert_safe(self) -> None:
        if self.unsafe:
            from live_mem.mesh.replay import ReplayError

            raise ReplayError("local_unsafe", "safe")

    async def admit_verified(self, envelope, **kwargs):
        self.calls.append({"envelope": envelope, **kwargs})
        if self.error is not None:
            raise self.error
        if self.decision is not None:
            return self.decision
        from live_mem.mesh.replay import ReplayDecision

        return ReplayDecision.ADMITTED


class FakeProcessLock:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired


_REPLAY_UNSET = object()


def _state(
    config,
    source_private: MeshPrivateKey,
    *,
    epoch: int = 7,
    healthy: bool = True,
    source_scopes: list[str] | None = None,
    source_node_id: str = "source-node",
) -> FakeStorage:
    local_member = Member(
        node_id="local-node",
        public_key=config.public_key,
        scopes=["commit", "propose", "read"],
    )
    source_member = Member(
        node_id=source_node_id,
        public_key=source_private.public_key(),
        scopes=source_scopes or ["propose", "read"],
    )
    membership = MembershipView(epoch=epoch, members=[local_member, source_member])
    node = NodeIdentity(node_id="local-node", public_key=config.public_key)
    health = NodeHealth(
        status=HiveNodeStatus.HEALTHY if healthy else HiveNodeStatus.UNSAFE
    )
    return FakeStorage(
        {
            layout.node_key("space-one"): node.model_dump_json(),
            layout.members_key("space-one"): membership.model_dump_json(),
            layout.node_status_key("space-one"): health.model_dump_json(),
        }
    )


def _event_body(*, epoch: int = 7, origin: str = "source-node") -> bytes:
    event = EventEnvelope(
        event_id="event-one",
        request_id=REQUEST_ID,
        type=EventType.TOKEN_CLAIM,
        created_at="2026-07-15T16:00:00+00:00",
        origin_node_id=origin,
        term=2,
        membership_epoch=epoch,
        bank_version=3,
        payload={"reason": "bounded"},
    )
    return canonical_dumps(event.model_dump(mode="json"))


def _signed_scope(
    config,
    source_private: MeshPrivateKey,
    *,
    operation: MeshHttpOperation = MeshHttpOperation.EVENTS,
    body: bytes | None = None,
    issued_at_ms: int = NOW_MS,
    membership_epoch: int = 7,
    target_fingerprint: str | None = None,
    signature_override: bytes | None = None,
) -> dict[str, Any]:
    route = MESH_ROUTES[operation]
    request_id = REQUEST_ID if operation is MeshHttpOperation.EVENTS else PAIR_ID
    path = route.path_for(PAIR_ID if route.pair_id_in_path else None)
    request_body = (
        _event_body(epoch=membership_epoch)
        if body is None and operation is MeshHttpOperation.EVENTS
        else (body or b"")
    )
    envelope = MeshRequestEnvelope.create(
        op=operation,
        path=path,
        space_id="space-one",
        source_public_key=source_private.public_key(),
        source_fingerprint=mesh_identity_fingerprint(source_private.public_key()),
        target_fingerprint=target_fingerprint or config.fingerprint,
        membership_epoch=membership_epoch,
        request_id=request_id,
        nonce=NONCE,
        issued_at_ms=issued_at_ms,
        body=request_body,
    )
    signature = signature_override or envelope.sign(source_private)
    headers = [*mesh_headers(envelope, signature)]
    if route.method == "POST":
        headers.extend(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(request_body)).encode("ascii")),
            ]
        )
    return {
        "type": "http",
        "method": route.method,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "_body": request_body,
    }


async def _invoke(app, scope: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            raise AssertionError("body requested more than once")
        consumed = True
        return {"type": "http.request", "body": scope.pop("_body", b"")}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    return messages


def _status(messages: list[dict[str, Any]]) -> int:
    return messages[0]["status"]


def _body(messages: list[dict[str, Any]]) -> bytes:
    return messages[1]["body"]


def _signed_code(messages: list[dict[str, Any]]) -> MeshResponseCode:
    start = messages[0]
    envelope, signature = MeshResponseEnvelope.from_headers(start["headers"])
    envelope.verify(signature)
    envelope.bind_response(status=start["status"], body=_body(messages))
    assert envelope.acknowledged is False
    return envelope.code


def _router(config, storage, replay=_REPLAY_UNSET, fallback=None):
    async def default_fallback(scope, receive, send):
        del scope, receive
        await send({"type": "http.response.start", "status": 299, "headers": []})
        await send({"type": "http.response.body", "body": b"fallback"})

    return MeshNamespaceRouter(
        fallback or default_fallback,
        config=config,
        process_lock=FakeProcessLock(),
        storage_factory=lambda: storage,
        replay_ledger=(
            FakeReplayLedger() if replay is _REPLAY_UNSET else replay
        ),
        clock_ms=lambda: NOW_MS,
        nonce_factory=lambda: "nonce_" + "9" * 64,
    )


@pytest.mark.asyncio
async def test_namespace_is_broad_but_non_mesh_falls_through() -> None:
    config = _config()
    router = _router(config, FakeStorage())
    assert MeshNamespaceRouter.is_mesh_namespace(
        {"type": "http", "path": "/mesh/v10", "raw_path": b"/mesh/v10"}
    )
    assert MeshNamespaceRouter.is_mesh_namespace(
        {"type": "http", "path": "/mesh/v1/events", "raw_path": b"/mesh%2fv1/events"}
    )
    messages = await _invoke(
        router,
        {
            "type": "http",
            "method": "GET",
            "path": "/health",
            "raw_path": b"/health",
            "query_string": b"",
            "headers": [],
            "_body": b"",
        },
    )
    assert _status(messages) == 299


@pytest.mark.asyncio
async def test_process_lock_lost_after_construction_refuses_before_receive_or_io() -> None:
    config = _config()
    source = _private(8)
    storage = FakeStorage()
    process_lock = FakeProcessLock()

    async def fallback(scope, receive, send):  # pragma: no cover - must not run
        del scope, receive, send
        raise AssertionError("Mesh namespace fell through")

    router = MeshNamespaceRouter(
        fallback,
        config=config,
        process_lock=process_lock,
        storage_factory=lambda: storage,
        replay_ledger=FakeReplayLedger(),
        clock_ms=lambda: NOW_MS,
        nonce_factory=lambda: "nonce_" + "9" * 64,
    )
    scope = _signed_scope(config, source)
    messages: list[dict[str, Any]] = []
    received = False

    async def receive():
        nonlocal received
        received = True
        raise AssertionError("request body must not be consumed")

    async def send(message):
        messages.append(message)

    process_lock.acquired = False
    await router(scope, receive, send)

    assert _status(messages) == 503
    assert b"hivemind-mesh-envelope" not in dict(messages[0]["headers"])
    assert received is False
    assert storage.reads == []
    assert storage.writes == []


@pytest.mark.asyncio
async def test_auth_bypass_exists_only_with_explicit_namespace_callback() -> None:
    async def inner(scope, receive, send):
        del scope, receive
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mesh/v1/events",
        "raw_path": b"/mesh/v1/events",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "_body": b"",
    }
    assert _status(await _invoke(AuthMiddleware(inner), dict(scope))) == 401
    enabled = AuthMiddleware(inner, peer_namespace=MeshNamespaceRouter.is_mesh_namespace)
    assert _status(await _invoke(enabled, dict(scope))) == 204


@pytest.mark.asyncio
async def test_pair_skeleton_is_signed_and_touches_no_storage_or_replay() -> None:
    config = _config()
    source = _private(8)
    storage = FakeStorage()
    replay = FakeReplayLedger()
    scope = _signed_scope(
        config,
        source,
        operation=MeshHttpOperation.PAIR_STATUS,
        body=b"",
    )
    messages = await _invoke(_router(config, storage, replay), scope)
    assert _status(messages) == 503
    assert _signed_code(messages) is MeshResponseCode.OPERATION_UNAVAILABLE
    assert storage.reads == []
    assert storage.writes == []
    assert replay.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda scope: scope.update(path="/mesh/v1/unknown", raw_path=b"/mesh/v1/unknown"), 404),
        (lambda scope: scope.update(method="GET"), 405),
        (lambda scope: scope.update(raw_path=b"/mesh/v1/%65vents"), 400),
        (lambda scope: scope.update(query_string=b"x=1"), 400),
        (
            lambda scope: scope.update(
                path="/mesh/v1/pair/not-a-pair/status",
                raw_path=b"/mesh/v1/pair/not-a-pair/status",
            ),
            400,
        ),
    ],
)
async def test_edge_path_and_method_matrix_is_unsigned(mutation, expected) -> None:
    config = _config()
    scope = _signed_scope(config, _private(8))
    mutation(scope)
    messages = await _invoke(_router(config, FakeStorage()), scope)
    assert _status(messages) == expected
    assert b"hivemind-mesh-envelope" not in dict(messages[0]["headers"])


@pytest.mark.asyncio
async def test_header_and_body_limits_pass_at_boundary_and_reject_plus_one() -> None:
    config = _config(control_max_bytes=8)
    router = _router(config, FakeStorage())
    base = {
        "type": "http",
        "method": "POST",
        "path": "/mesh/v1/events",
        "raw_path": b"/mesh/v1/events",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "_body": b"x" * 8,
    }
    assert _status(await _invoke(router, dict(base))) == 400  # auth headers absent
    over = dict(base)
    over["_body"] = b"x" * 9
    assert _status(await _invoke(router, over)) == 413

    header_boundary = dict(base)
    header_boundary["headers"] = [
        (b"a", b"b") for _ in range(64)
    ]
    # Duplicate headers are ambiguous before auth.
    assert _status(await _invoke(router, header_boundary)) == 400
    header_over = dict(base)
    header_over["headers"] = [
        (f"x-{index}".encode(), b"v") for index in range(65)
    ]
    assert _status(await _invoke(router, header_over)) == 431

    # Four unique headers total exactly 16,384 bytes under the frozen wire
    # accounting.  They pass the header budget and fail later at framing; one
    # additional byte is therefore distinguishable as a 431 edge refusal.
    exact_headers = [
        (b"a", b"x" * 4096),
        (b"b", b"x" * 4096),
        (b"c", b"x" * 4096),
        (b"d", b"x" * 4076),
    ]
    assert sum(len(n) + 2 + len(v) + 2 for n, v in exact_headers) == 16_384
    exact = dict(base)
    exact["headers"] = exact_headers
    assert _status(await _invoke(router, exact)) == 400
    plus_one = dict(base)
    plus_one["headers"] = [*exact_headers[:-1], (b"d", b"x" * 4077)]
    assert _status(await _invoke(router, plus_one)) == 431


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["signature", "target", "stale", "digest"])
async def test_self_proof_failures_share_one_unsigned_401(failure: str) -> None:
    config = _config()
    source = _private(8)
    kwargs: dict[str, Any] = {}
    if failure == "signature":
        kwargs["signature_override"] = b"\x00" * 64
    elif failure == "target":
        kwargs["target_fingerprint"] = mesh_identity_fingerprint(_private(9).public_key())
    elif failure == "stale":
        kwargs["issued_at_ms"] = NOW_MS - 300_001
    scope = _signed_scope(config, source, **kwargs)
    if failure == "digest":
        scope["_body"] += b"x"
        scope["headers"] = [
            (name, str(len(scope["_body"])).encode() if name == b"content-length" else value)
            for name, value in scope["headers"]
        ]
    messages = await _invoke(_router(config, FakeStorage()), scope)
    assert _status(messages) == 401
    assert _body(messages) == canonical_dumps({"error": "MESH_REQUEST_REJECTED"})


@pytest.mark.asyncio
async def test_invalid_event_is_signed_before_any_storage_access() -> None:
    config = _config()
    storage = FakeStorage()
    messages = await _invoke(
        _router(config, storage),
        _signed_scope(config, _private(8), body=canonical_dumps({"bad": True})),
    )
    assert _status(messages) == 400
    assert _signed_code(messages) is MeshResponseCode.INVALID_EVENT
    assert storage.reads == []


@pytest.mark.asyncio
async def test_invalid_event_precedes_poisoned_replay_capability() -> None:
    config = _config()
    storage = FakeStorage()
    replay = FakeReplayLedger(unsafe=True)
    messages = await _invoke(
        _router(config, storage, replay),
        _signed_scope(
            config,
            _private(8),
            body=canonical_dumps({"bad": True}),
        ),
    )
    assert _status(messages) == 400
    assert _signed_code(messages) is MeshResponseCode.INVALID_EVENT
    assert storage.reads == []
    assert storage.writes == []
    assert replay.calls == []


@pytest.mark.asyncio
async def test_local_health_fails_closed_before_source_or_epoch_or_replay() -> None:
    config = _config()
    source = _private(8)
    storage = _state(config, source, healthy=False, epoch=99)
    replay = FakeReplayLedger()
    messages = await _invoke(
        _router(config, storage, replay),
        _signed_scope(config, source, membership_epoch=7),
    )
    assert _status(messages) == 423
    assert _signed_code(messages) is MeshResponseCode.LOCAL_UNSAFE
    assert replay.calls == []
    assert storage.writes == []


@pytest.mark.asyncio
async def test_source_authorization_precedes_epoch_and_replay() -> None:
    config = _config()
    source = _private(8)
    storage = _state(config, source, epoch=99, source_scopes=["read"])
    replay = FakeReplayLedger()
    messages = await _invoke(
        _router(config, storage, replay),
        _signed_scope(config, source, membership_epoch=7),
    )
    assert _status(messages) == 403
    assert _signed_code(messages) is MeshResponseCode.SOURCE_NOT_AUTHORIZED
    assert replay.calls == []


@pytest.mark.asyncio
async def test_poisoned_replay_capability_masks_source_and_epoch_oracles() -> None:
    config = _config()
    source = _private(8)
    storage = _state(config, source, epoch=99, source_scopes=["read"])
    replay = FakeReplayLedger(unsafe=True)
    messages = await _invoke(
        _router(config, storage, replay),
        _signed_scope(config, source, membership_epoch=7),
    )
    assert _status(messages) == 423
    assert _signed_code(messages) is MeshResponseCode.LOCAL_UNSAFE
    assert replay.calls == []
    assert storage.reads == []
    assert storage.writes == []


@pytest.mark.asyncio
async def test_epoch_mismatch_precedes_replay() -> None:
    config = _config()
    source = _private(8)
    storage = _state(config, source, epoch=8)
    replay = FakeReplayLedger()
    messages = await _invoke(
        _router(config, storage, replay),
        _signed_scope(config, source, membership_epoch=7),
    )
    assert _status(messages) == 409
    assert _signed_code(messages) is MeshResponseCode.EPOCH_MISMATCH
    assert replay.calls == []


@pytest.mark.asyncio
async def test_admitted_event_writes_no_business_state_and_returns_signed_503() -> None:
    config = _config()
    source = _private(8)
    storage = _state(config, source)
    replay = FakeReplayLedger()
    messages = await _invoke(
        _router(config, storage, replay),
        _signed_scope(config, source),
    )
    assert _status(messages) == 503
    assert _signed_code(messages) is MeshResponseCode.OPERATION_UNAVAILABLE
    assert len(replay.calls) == 1
    assert storage.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision,error,expected_code",
    [
        pytest.param("duplicate", None, MeshResponseCode.REPLAY_REJECTED, id="duplicate"),
        pytest.param(None, "replay_conflict", MeshResponseCode.REPLAY_REJECTED, id="conflict"),
        pytest.param(None, "replay_saturated", MeshResponseCode.LOCAL_UNSAFE, id="saturated"),
        pytest.param(None, "local_unsafe", MeshResponseCode.LOCAL_UNSAFE, id="unsafe"),
        pytest.param(None, "storage_timeout", MeshResponseCode.LOCAL_UNSAFE, id="timeout"),
    ],
)
async def test_replay_outcomes_are_signed_and_fail_before_business_mutation(
    decision: str | None,
    error: str | None,
    expected_code: MeshResponseCode,
) -> None:
    from live_mem.mesh.replay import ReplayDecision, ReplayError

    config = _config()
    source = _private(8)
    storage = _state(config, source)
    replay = FakeReplayLedger(
        decision=(ReplayDecision.DUPLICATE if decision == "duplicate" else None),
        error=(ReplayError(error, "safe") if error is not None else None),
    )
    messages = await _invoke(
        _router(config, storage, replay),
        _signed_scope(config, source),
    )
    assert _signed_code(messages) is expected_code
    assert storage.writes == []


@pytest.mark.asyncio
async def test_default_replay_is_durable_across_router_restart() -> None:
    config = _config()
    source = _private(8)
    storage = _state(config, source)
    scope = _signed_scope(config, source)

    first = await _invoke(_router(config, storage, replay=None), dict(scope))
    assert _status(first) == 503
    assert _signed_code(first) is MeshResponseCode.OPERATION_UNAVAILABLE
    replay_writes = [
        key
        for key, value in storage.writes
        if value is not None
        and key.startswith(
            f"_system/mesh_pairing/{config.fingerprint}/replay/"
        )
    ]
    assert len(replay_writes) == 1

    # A fresh router has no in-memory replay cache.  It must reload the durable
    # record and reject the exact signed request before any new write.
    writes_before_restart = list(storage.writes)
    restarted = await _invoke(
        _router(config, storage, replay=None),
        dict(scope),
    )
    assert _status(restarted) == 409
    assert _signed_code(restarted) is MeshResponseCode.REPLAY_REJECTED
    assert storage.writes == writes_before_restart


@pytest.mark.asyncio
async def test_freshness_boundary_is_inclusive() -> None:
    config = _config()
    source = _private(8)
    storage = _state(config, source)
    for issued_at in (NOW_MS - 300_000, NOW_MS + 300_000):
        messages = await _invoke(
            _router(config, storage, FakeReplayLedger()),
            _signed_scope(config, source, issued_at_ms=issued_at),
        )
        assert _status(messages) == 503
