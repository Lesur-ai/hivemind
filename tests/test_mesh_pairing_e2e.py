# -*- coding: utf-8 -*-
"""Two-instance Project Mesh pairing end-to-end proof (P10-3, issue #191).

Drives two REAL ASGI ``MeshNamespaceRouter`` instances (source A, target B)
through the three actions and asserts they converge on membership, epoch, bank
version, and node health — then replicates a subsequent shared mutation over the
paired mesh. This is the P10-3 acceptance criterion.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import uvicorn

from live_mem.core.hivemind import (
    BankCommit,
    BankVersionPointer,
    HiveNodeStatus,
    HivemindStateStore,
    Member,
    MembershipView,
    NodeHealth,
    NodeIdentity,
    TokenLeaseState,
    TokenState,
)
from live_mem.core.hivemind import layout
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
from live_mem.mesh.canonical import canonical_dumps
from live_mem.mesh.identity import (
    MESH_PRIVATE_KEY_PREFIX,
    decode_mesh_public_key,
    generate_mesh_identity,
    parse_mesh_private_key,
)
from live_mem.mesh.pairing_client import PeerResponse
from live_mem.mesh.pairing_service import MeshPairingService, MeshPairingServiceError
from live_mem.mesh.pairing_state import (
    MeshPairingState,
    SignedSourceBootstrapEvidence,
    SignedTargetTerminalConfirmationReceipt,
)
from live_mem.mesh.router import MeshNamespaceRouter
from tests.test_hivemind_state import FakeStorage
from tests.test_mesh_router import FakeProcessLock, FakeReplayLedger, _invoke

NOW_MS = 1_800_000_000_000
SPACE = "meshspace"
A_URL = "https://a.mesh.test"
B_URL = "https://b.mesh.test"

# These proofs deliberately schedule nested ASGI peer exchanges while holding
# test-controlled events.  They detect a deadlock, rather than asserting a
# performance budget, so leave enough headroom for coverage-instrumented CI
# runners without allowing a genuinely stuck task to hang the suite.
ASYNC_RACE_TIMEOUT_SECONDS = 5


def _config(private, url: str):
    config = load_mesh_config(
        {
            MESH_ENABLED_ENV: "true",
            MESH_PUBLIC_URL_ENV: url,
            MESH_PRIVATE_KEY_ENV: private,
            MESH_DISPLAY_NAME_ENV: "peer",
            MESH_INVITATION_TTL_ENV: "3600",
            MESH_CONTROL_MAX_BYTES_ENV: "262144",
            MESH_BOOTSTRAP_MAX_BYTES_ENV: "268435456",
            MESH_BOOTSTRAP_MAX_OBJECTS_ENV: "50000",
        }
    )
    assert config is not None
    return config


def _legacy(mesh_public_key: str) -> str:
    raw = decode_mesh_public_key(mesh_public_key)
    return "ed25519:" + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class AsgiPeerSender:
    """Routes a pairing client's request straight into a peer's ASGI router."""

    def __init__(self, peers: dict, key: str) -> None:
        self._peers = peers
        self._key = key

    async def send(self, method, path, *, headers, body) -> PeerResponse:
        h = list(headers)
        if method == "POST":
            h.append((b"content-type", b"application/json"))
            h.append((b"content-length", str(len(body)).encode("ascii")))
        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": h,
            "client": ("127.0.0.1", 1),
            "_body": body,
        }
        messages = await _invoke(self._peers[self._key], dict(scope))
        status = messages[0]["status"]
        resp_headers = messages[0]["headers"]
        resp_body = messages[1]["body"] if len(messages) > 1 else b""
        return PeerResponse(status, resp_headers, resp_body)


async def _seed_source(storage: FakeStorage, config) -> None:
    # Legacy pairing fixtures used to seed only protocol objects. Source
    # readiness now (correctly) requires the same committed-space marker as the
    # product, so layer legacy Hivemind state onto a real committed space.
    await _seed_blank_target(storage)
    store = HivemindStateStore(storage=storage, space_id=SPACE)  # type: ignore[arg-type]
    legacy = _legacy(config.public_key)
    node_id = "sourcenode0000000000000000000000"
    await store.set_node_identity(NodeIdentity(node_id=node_id, public_key=legacy))
    await store.set_membership(
        MembershipView(
            epoch=1,
            members=[Member(node_id=node_id, public_key=legacy, scopes=None)],
        )
    )
    await store.bump_term(2, updated_by_node_id=node_id)
    await store.append_commit(
        BankCommit(bank_version=1, parent_bank_version=0, term=2, commit_id="c1", committed_by_node_id=node_id)
    )
    await store.set_bank_version_pointer(BankVersionPointer(bank_version=1, commit_id="c1"))
    # A little shared content so the snapshot is non-trivial.
    await storage.put(f"{SPACE}/bank/activeContext.md", "# Active\ncontext")


async def _seed_blank_target(storage: FakeStorage) -> None:
    import json

    await storage.put(f"{SPACE}/_meta.json", json.dumps({"space_id": SPACE, "version": 1}))
    await storage.put(f"{SPACE}/_rules.md", "")
    await storage.put(f"{SPACE}/live/.keep", "")
    await storage.put(f"{SPACE}/bank/.keep", "")


def _fallback():
    async def fb(scope, receive, send):
        del scope, receive
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    return fb


class _AdminAuthenticatedApp:
    """Test-only auth boundary for a real admin ASGI request.

    ``MeshAdminMiddleware`` deliberately receives its authenticated identity
    from the production auth middleware.  This wrapper supplies that trusted
    context *inside* each test server request, so the proof still traverses
    TCP, HTTP parsing, the admin route, and the signed peer routes without
    pretending that a bearer-less request is an administrator.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        from live_mem.auth.context import current_token_info

        token = current_token_info.set(
            {"permissions": ["admin", "read", "write"], "client_name": "e2e-admin"}
        )
        try:
            await self._app(scope, receive, send)
        finally:
            current_token_info.reset(token)


@contextlib.asynccontextmanager
async def _loopback_asgi_server(app) -> AsyncIterator[str]:
    """Serve one ASGI app on an ephemeral loopback TCP socket.

    This is intentionally not an ASGI-transport shortcut: callers below open
    a new TCP connection for every HTTP request.  Plain HTTP is allowed only
    by this test's literal loopback seam; production peer destinations remain
    HTTPS-only in ``HttpPeerTransport``.
    """

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="critical", access_log=False)
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started and server.servers:
                socket = server.servers[0].sockets[0]
                host, port = socket.getsockname()[:2]
                yield f"http://{host}:{port}"
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - environment startup failure, not a protocol outcome
            raise RuntimeError("loopback ASGI server did not start")
    finally:
        server.should_exit = True
        await task


async def _tcp_http_request(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: tuple[tuple[bytes, bytes], ...] = (),
    body: bytes = b"",
) -> PeerResponse:
    """Minimal HTTP/1.1 client used only to prove the real TCP boundary."""

    parsed = urlsplit(base_url)
    assert parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
    try:
        raw_headers = [
            (b"host", f"{parsed.hostname}:{parsed.port}".encode("ascii")),
            (b"connection", b"close"),
            *headers,
        ]
        if body:
            raw_headers.append((b"content-type", b"application/json"))
        raw_headers.append((b"content-length", str(len(body)).encode("ascii")))
        request = bytearray(f"{method} {path} HTTP/1.1\r\n".encode("ascii"))
        request.extend(b"".join(name + b": " + value + b"\r\n" for name, value in raw_headers))
        request.extend(b"\r\n")
        request.extend(body)
        writer.write(request)
        await writer.drain()

        head = await reader.readuntil(b"\r\n\r\n")
        lines = head[:-4].split(b"\r\n")
        status = int(lines[0].split(b" ", 2)[1])
        response_headers = []
        for line in lines[1:]:
            name, value = line.split(b":", 1)
            response_headers.append((name.lower(), value.strip()))
        length = next((int(value) for name, value in response_headers if name == b"content-length"), 0)
        response_body = await reader.readexactly(length) if length else b""
        return PeerResponse(status, response_headers, response_body)
    finally:
        writer.close()
        await writer.wait_closed()


class _LoopbackHttpSender:
    """A peer sender that can only reach the real loopback HTTP listener."""

    def __init__(self, target_url: str, transcript: list[tuple[str, str]]) -> None:
        self._target_url = target_url
        self._transcript = transcript

    async def send(self, method, path, *, headers, body) -> PeerResponse:
        self._transcript.append((method, path))
        return await _tcp_http_request(self._target_url, method, path, headers=tuple(headers), body=body)


async def _admin_tcp_post(base_url: str, action: str, data: dict) -> tuple[int, dict]:
    body = json.dumps({"confirm": True, **data}).encode("utf-8")
    response = await _tcp_http_request(
        base_url,
        "POST",
        f"/api/admin/mesh/{action}",
        headers=((b"origin", base_url.encode("ascii")),),
        body=body,
    )
    return response.status_code, json.loads(response.body)


async def test_two_tcp_asgi_admins_pair_without_in_process_peer_transport(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-5: prove the three actions cross two real ASGI/TCP boundaries.

    The older P10-3 tests intentionally use an in-process sender to make the
    state-machine fault corpus deterministic.  This separate convergence test
    prevents that convenience seam from being mistaken for a network proof:
    every admin and peer request below is a fresh loopback TCP connection.
    """

    from live_mem.mesh.mesh_admin import MeshAdminMiddleware
    from live_mem.middleware import AuditMiddleware

    a_private = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([111]) * 32).decode().rstrip("=")
    b_private = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([112]) * 32).decode().rstrip("=")
    a_config, b_config = _config(a_private, A_URL), _config(b_private, B_URL)
    a_storage, b_storage = FakeStorage(), FakeStorage()
    # Start from the real product creation path: no private node/membership seed.
    from live_mem.core import locks as locks_module
    from live_mem.core import space as space_module
    from live_mem.core import tokens as tokens_module
    from live_mem.core.locks import LockManager
    from live_mem.core.space import SpaceService
    from live_mem.core.tokens import TokenService

    monkeypatch.setattr(space_module, "get_storage", lambda: a_storage)
    monkeypatch.setattr(tokens_module, "get_storage", lambda: a_storage)
    monkeypatch.setattr(tokens_module, "_token_service", TokenService())
    monkeypatch.setattr(locks_module, "_lock_manager", LockManager())
    created = await SpaceService().create(
        SPACE,
        "TCP Project Mesh source",
        "# TCP source rules",
        owner="source-admin",
        bootstrap_admin=True,
    )
    assert created["status"] == "created"
    await a_storage.put(f"{SPACE}/bank/activeContext.md", "# TCP source content")
    business_before = {
        key: value
        for key, value in a_storage.snapshot().items()
        if key.startswith(f"{SPACE}/") and "/_hivemind/" not in key
    }
    await _seed_blank_target(b_storage)

    endpoints: dict[str, str] = {}
    a_transcript: list[tuple[str, str]] = []
    b_transcript: list[tuple[str, str]] = []
    invitation_canary = "P10-5-INVITATION-CANARY"
    snapshot_canary = "P10-5-SNAPSHOT-CANARY"
    clock = lambda: NOW_MS
    a_service = MeshPairingService(
        a_config,
        a_storage,
        clock_ms=clock,
        secret_factory=lambda: invitation_canary,
        sender_factory=lambda _endpoint: _LoopbackHttpSender(endpoints["B"], a_transcript),
    )
    b_service = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=clock,
        sender_factory=lambda _endpoint: _LoopbackHttpSender(endpoints["A"], b_transcript),
    )
    a_router = MeshNamespaceRouter(
        _fallback(), config=a_config, process_lock=FakeProcessLock(),
        storage_factory=lambda: a_storage, replay_ledger=FakeReplayLedger(),
        clock_ms=clock, pairing_service=a_service,
    )
    b_router = MeshNamespaceRouter(
        _fallback(), config=b_config, process_lock=FakeProcessLock(),
        storage_factory=lambda: b_storage, replay_ledger=FakeReplayLedger(),
        clock_ms=clock, pairing_service=b_service,
    )
    # Audit is inside the authenticated test boundary, as it is in the real
    # ASGI stack.  These records prove that neither admin nor peer request
    # bodies (and therefore no invitation/private-key/snapshot canary) enter
    # the structured audit trail.
    a_app = _AdminAuthenticatedApp(AuditMiddleware(MeshAdminMiddleware(a_router, a_service)))
    b_app = _AdminAuthenticatedApp(AuditMiddleware(MeshAdminMiddleware(b_router, b_service)))
    caplog.set_level("INFO", logger="live_mem.audit")

    async with _loopback_asgi_server(a_app) as a_url, _loopback_asgi_server(b_app) as b_url:
        endpoints.update(A=a_url, B=b_url)

        # #413 prerequisite: readiness and preparation also traverse the real
        # authenticated admin/TCP boundary. It must not mutate business bytes or
        # create an invitation as a hidden continuation.
        status_response = await _tcp_http_request(
            a_url, "GET", "/api/admin/mesh/status"
        )
        assert status_response.status_code == 200
        mesh_status = json.loads(status_response.body)
        source = next(
            item
            for item in mesh_status["source_readiness"]
            if item["space_id"] == SPACE
        )
        assert source["state"] == "local_only_can_prepare"
        status, prepared = await _admin_tcp_post(
            a_url,
            "prepare-source",
            {
                "space_id": SPACE,
                "quiesced": True,
                "expected_state_token": source["state_token"],
            },
        )
        assert status == 200 and prepared["result"] == "prepared"
        assert prepared["source"]["state"] == "ready"
        assert not await a_service.store.list_sessions()

        # Action 1: only this response may hold the display-once invitation
        # secret.  Durable pairing storage must retain the hash/digests only.
        status, invitation = await _admin_tcp_post(
            a_url,
            "invitation",
            {
                "space_id": SPACE,
                "scopes": ["read", "commit"],
                # Deliberately unknown payload fields are ignored by the strict
                # action handler, but would expose a body-logging regression.
                "private_key": a_private,
                "snapshot": snapshot_canary,
            },
        )
        assert status == 200 and invitation["status"] == "ok"
        secret = invitation["secret"]
        assert secret not in "\n".join(a_storage.objects.values())
        assert a_private not in "\n".join(a_storage.objects.values())

        # A hostile/accidental secret echoed into an error must not reach the
        # target's error body or its local operational storage.
        canary = "P10-5-LEAK-CANARY"
        status, refused = await _admin_tcp_post(
            b_url,
            "accept",
            {
                "invitation": invitation["invitation"], "target_space_id": SPACE,
                "secret": canary, "source_endpoint": A_URL, "scopes": ["read", "commit"],
                "quiesced": True,
            },
        )
        assert status == 400 and canary not in json.dumps(refused)
        assert canary not in "\n".join(b_storage.objects.values())

        # Action 2: the target reserves its blank space and claims via B -> A
        # signed HTTP.  Action 3 is the source approval plus target enrollment.
        status, accepted = await _admin_tcp_post(
            b_url,
            "accept",
            {
                "invitation": invitation["invitation"], "target_space_id": SPACE,
                "secret": secret, "source_endpoint": A_URL, "scopes": ["read", "commit"],
                "quiesced": True,
            },
        )
        assert status == 200 and accepted["state"] == "claimed"
        status, approved = await _admin_tcp_post(a_url, "approve", {"pair_id": invitation["pair_id"]})
        assert status == 200 and approved["epoch"] == 1
        status, enrolled = await _admin_tcp_post(b_url, "enroll", {"pair_id": invitation["pair_id"]})
        assert status == 200 and enrolled["state"] == "active"

    # The underlying stores agree at e+2, and each direction crossed at least
    # one actual signed peer route.  A direct ASGI call cannot satisfy either
    # transcript assertion because _LoopbackHttpSender owns the only sender.
    a_members = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    b_members = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    target_id = b_config.fingerprint.split(":", 1)[1]
    assert a_members.epoch == b_members.epoch == 2
    assert any(member.node_id == target_id and member.status == "active" for member in a_members.members)
    assert any(member.node_id == target_id and member.status == "active" for member in b_members.members)
    assert any(path == "/mesh/v1/pair/claim" for _method, path in b_transcript)
    assert any(path == "/mesh/v1/events" for _method, path in a_transcript)
    persisted = "\n".join([*a_storage.objects.values(), *b_storage.objects.values()])
    assert secret not in persisted and a_private not in persisted and b_private not in persisted
    assert snapshot_canary not in persisted
    business_after = {
        key: value
        for key, value in a_storage.snapshot().items()
        if key.startswith(f"{SPACE}/") and "/_hivemind/" not in key
    }
    assert business_after == business_before
    assert b_storage.objects[f"{SPACE}/bank/activeContext.md"] == "# TCP source content"

    audit_entries = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "live_mem.audit"
    ]
    assert any(entry["path"] == "/api/admin/mesh/invitation" for entry in audit_entries)
    assert any(entry["path"] == "/api/admin/mesh/prepare-source" for entry in audit_entries)
    assert any(entry["path"] == "/mesh/v1/pair/claim" for entry in audit_entries)
    audit_blob = json.dumps(audit_entries, sort_keys=True)
    for canary in (invitation_canary, a_private, b_private, snapshot_canary):
        assert canary not in audit_blob


async def test_two_asgi_instances_pair_and_converge(monkeypatch):
    # Distinct deterministic identities for A and B.
    a_priv = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([11]) * 32).decode().rstrip("=")
    b_priv = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([22]) * 32).decode().rstrip("=")
    a_config = _config(a_priv, A_URL)
    b_config = _config(b_priv, B_URL)
    assert a_config.fingerprint != b_config.fingerprint

    a_storage, b_storage = FakeStorage(), FakeStorage()
    await _seed_source(a_storage, a_config)
    await _seed_blank_target(b_storage)

    clock = lambda: NOW_MS
    peers: dict = {}
    a_service = MeshPairingService(
        a_config, a_storage, clock_ms=clock,
        sender_factory=lambda _e: AsgiPeerSender(peers, "B"),
    )
    b_service = MeshPairingService(
        b_config, b_storage, clock_ms=clock,
        sender_factory=lambda _e: AsgiPeerSender(peers, "A"),
    )
    a_router = MeshNamespaceRouter(
        _fallback(), config=a_config, process_lock=FakeProcessLock(),
        storage_factory=lambda: a_storage, replay_ledger=FakeReplayLedger(),
        clock_ms=clock, pairing_service=a_service,
    )
    b_router = MeshNamespaceRouter(
        _fallback(), config=b_config, process_lock=FakeProcessLock(),
        storage_factory=lambda: b_storage, replay_ledger=FakeReplayLedger(),
        clock_ms=clock, pairing_service=b_service,
    )
    peers["A"], peers["B"] = a_router, b_router

    # Action 1: A creates a one-time invitation.
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "propose", "commit"))
    pair_id = invite["pair_id"]

    # Action 2: B accepts (reserves + sends the signed claim to A's router).
    accept = await b_service.accept_invitation(
        invite["invitation_bytes"], SPACE,
        secret=invite["secret"], source_endpoint=A_URL,
        requested_scopes=("read", "propose", "commit"), quiesced=True,
    )
    assert accept["state"] == "claimed"
    # A now has a claimed source session.
    a_session = await a_service.store.get_session(pair_id)
    assert a_session is not None and a_session.state == "claimed"

    # Action 3 part 1: A approves -> admit pending (e -> e+1) + export bootstrap.
    approved = await a_service.approve(pair_id)
    assert approved["epoch"] == 2

    # Action 3 part 2: B drives status -> bootstrap import -> final ACK, which
    # makes A promote (e+1 -> e+2) and deliver the activation back to B, which
    # self-activates via the confined router branch.
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] == "active", result

    # --- Convergence assertions --------------------------------------------
    a_membership = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    b_membership = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_membership.epoch == 3 and b_membership.epoch == 3  # exact e+2 both sides
    target_node = b_config.fingerprint.split(":", 1)[1]
    assert any(m.node_id == target_node and m.status == "active" for m in a_membership.members)
    assert any(m.node_id == target_node and m.status == "active" for m in b_membership.members)
    b_health = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert HiveNodeStatus(b_health.status) == HiveNodeStatus.HEALTHY
    a_final = await a_service.store.get_session(pair_id)
    assert a_final.state == "active"

    # --- Reservation released on activation: the paired target accepts writes -
    from live_mem.core.reservation_guard import (
        assert_space_not_reserved,
        clear_reservation_checker,
        register_reservation_checker,
    )

    assert await b_service.store.get_reservation(SPACE) is None
    register_reservation_checker(b_service.assert_space_not_reserved)
    try:
        await assert_space_not_reserved(SPACE)  # must NOT raise — no longer reserved
    finally:
        clear_reservation_checker()

    # --- Subsequent shared mutation over the now-paired mesh ----------------
    # A delivers a real same-epoch shared event to B; B (now an active member at
    # e+2) applies it through the general events pipeline, proving the paired
    # mesh carries ongoing shared mutations.
    from live_mem.core.hivemind import EventEnvelope, EventType
    from live_mem.mesh.canonical import canonical_dumps
    from live_mem.mesh.pairing_client import MeshPairingClient
    from live_mem.mesh.secret import generate_request_id

    a_node_id = "sourcenode0000000000000000000000"
    req = generate_request_id()
    shared_event = EventEnvelope(
        event_id="sharedmutation1",
        request_id=req,
        type=EventType.RESYNC_COMPLETED,
        origin_node_id=a_node_id,
        term=2,
        membership_epoch=3,
        payload={"marker": "shared-after-pairing"},
    )
    body = canonical_dumps(shared_event.model_dump(mode="json"))
    a_to_b = MeshPairingClient(
        AsgiPeerSender(peers, "B"),
        source_public_key=a_config.public_key,
        source_fingerprint=a_config.fingerprint,
        private_key=a_config.private_key,
        clock_ms=clock,
    )
    resp = await a_to_b.deliver_event(
        space_id=SPACE, epoch=3, target_fingerprint=b_config.fingerprint, body=body, request_id=req
    )
    assert resp.status_code == 202  # ACCEPTED
    b_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    assert await b_store.has_event("sharedmutation1")


async def test_target_loss_after_final_ack_recovers_on_resume(monkeypatch):
    """If the source's e+2 activation delivery is lost after the target's final
    ACK, the target is stranded PENDING/blocked and converges on idempotent
    re-delivery (resume) — never abandoned, never silently rolled back."""

    a_priv = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([31]) * 32).decode().rstrip("=")
    b_priv = MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([32]) * 32).decode().rstrip("=")
    a_config = _config(a_priv, A_URL)
    b_config = _config(b_priv, B_URL)
    a_storage, b_storage = FakeStorage(), FakeStorage()
    await _seed_source(a_storage, a_config)
    await _seed_blank_target(b_storage)

    clock = lambda: NOW_MS
    peers: dict = {}
    a_service = MeshPairingService(a_config, a_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "B"))
    b_service = MeshPairingService(b_config, b_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    a_router = MeshNamespaceRouter(_fallback(), config=a_config, process_lock=FakeProcessLock(), storage_factory=lambda: a_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=a_service)

    # B's router DROPS the inbound e+2 activation event (delivery lost), so the
    # target never applies e+2 during the ACK round-trip.
    class _DroppingReplay(FakeReplayLedger):
        pass

    b_router = MeshNamespaceRouter(_fallback(), config=b_config, process_lock=FakeProcessLock(), storage_factory=lambda: b_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=b_service)
    peers["A"], peers["B"] = a_router, b_router

    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)
    await a_service.approve(pair_id)

    # Simulate lost activation delivery: temporarily point A's outbound sender at
    # a black hole so handle_ack's deliver_event fails -> source blocked_recovery,
    # target stranded PENDING at e+1.
    class _BlackHole:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(503, [], b"")

    peers_blackhole = {"B": None}
    a_service._sender_factory = lambda _e: _BlackHole()  # type: ignore[attr-defined]
    result = await b_service.run_target_enrollment(pair_id)
    # Target sent its final ACK but activation never arrived: still not active.
    assert result["state"] != "active"
    a_blocked = await a_service.store.get_session(pair_id)
    assert a_blocked.state == "blocked_recovery"
    # Blocked recovery persists SIGNED evidence (verifiable, no silent rollback).
    signed_ev = await a_service.store.get_evidence(pair_id)
    assert signed_ev is not None
    signed_ev.verify(a_config.public_key)
    assert signed_ev.evidence.next_action == "resume"
    b_target = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    tgt = b_config.fingerprint.split(":", 1)[1]
    assert any(m.node_id == tgt and m.status == "pending" for m in b_target.members)

    # Resume: restore delivery and idempotently re-drive the e+2 activation.
    a_service._sender_factory = lambda _e: AsgiPeerSender(peers, "B")  # type: ignore[attr-defined]
    resumed = await a_service.resume(pair_id)
    assert resumed["state"] == "active"
    a_conv = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    b_conv = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_conv.epoch == 3 and b_conv.epoch == 3
    assert any(m.node_id == tgt and m.status == "active" for m in b_conv.members)


async def _drive_to_approved(a_seed, b_seed):
    a_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([a_seed]) * 32).decode().rstrip("="), A_URL)
    b_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([b_seed]) * 32).decode().rstrip("="), B_URL)
    a_storage, b_storage = FakeStorage(), FakeStorage()
    await _seed_source(a_storage, a_config)
    await _seed_blank_target(b_storage)
    clock = lambda: NOW_MS
    peers: dict = {}
    a_service = MeshPairingService(a_config, a_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "B"))
    b_service = MeshPairingService(b_config, b_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    a_router = MeshNamespaceRouter(_fallback(), config=a_config, process_lock=FakeProcessLock(), storage_factory=lambda: a_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=a_service)
    b_router = MeshNamespaceRouter(_fallback(), config=b_config, process_lock=FakeProcessLock(), storage_factory=lambda: b_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=b_service)
    peers["A"], peers["B"] = a_router, b_router
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)
    await a_service.approve(pair_id)
    return a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id


async def _advance_completed_pair_head(a_storage, b_storage, *, commit_id: str):
    """Advance both peers once after all-ACK without changing membership."""

    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    source_node = await source_store.get_node_identity()
    assert source_node is not None
    for store in (source_store, target_store):
        membership = await store.get_membership()
        term = await store.get_term()
        assert membership is not None and term is not None
        commit = BankCommit(
            bank_version=2,
            parent_bank_version=1,
            term=term.term,
            membership_epoch=membership.epoch,
            commit_id=commit_id,
            committed_by_node_id=source_node.node_id,
            manifest=[],
        )
        await store.append_commit(commit)
        await store.set_bank_version_pointer(
            BankVersionPointer(bank_version=2, commit_id=commit_id)
        )
    return source_store, target_store


async def _drive_prepared_source_to_approved(a_seed, b_seed):
    """Reach e+1 from the real existing-space preparation path."""

    a_config = _config(
        MESH_PRIVATE_KEY_PREFIX
        + base64.urlsafe_b64encode(bytes([a_seed]) * 32).decode().rstrip("="),
        A_URL,
    )
    b_config = _config(
        MESH_PRIVATE_KEY_PREFIX
        + base64.urlsafe_b64encode(bytes([b_seed]) * 32).decode().rstrip("="),
        B_URL,
    )
    a_storage, b_storage = FakeStorage(), FakeStorage()
    await _seed_blank_target(a_storage)
    await _seed_blank_target(b_storage)
    clock = lambda: NOW_MS
    peers: dict = {}
    a_service = MeshPairingService(
        a_config,
        a_storage,
        clock_ms=clock,
        sender_factory=lambda _endpoint: AsgiPeerSender(peers, "B"),
    )
    b_service = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=clock,
        sender_factory=lambda _endpoint: AsgiPeerSender(peers, "A"),
    )
    a_router = MeshNamespaceRouter(
        _fallback(),
        config=a_config,
        process_lock=FakeProcessLock(),
        storage_factory=lambda: a_storage,
        replay_ledger=FakeReplayLedger(),
        clock_ms=clock,
        pairing_service=a_service,
    )
    b_router = MeshNamespaceRouter(
        _fallback(),
        config=b_config,
        process_lock=FakeProcessLock(),
        storage_factory=lambda: b_storage,
        replay_ledger=FakeReplayLedger(),
        clock_ms=clock,
        pairing_service=b_service,
    )
    peers["A"], peers["B"] = a_router, b_router

    readiness = await a_service.inspect_source_eligibility(SPACE)
    assert readiness["state"] == "local_only_can_prepare"
    prepared = await a_service.prepare_source(
        SPACE, expected_state_token=readiness["state_token"], quiesced=True
    )
    assert prepared["result"] == "prepared"
    invitation = await a_service.create_invitation(
        SPACE, requested_scopes=("read", "commit")
    )
    pair_id = invitation["pair_id"]
    accepted = await b_service.accept_invitation(
        invitation["invitation_bytes"],
        SPACE,
        secret=invitation["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read", "commit"), quiesced=True,
    )
    assert accepted["state"] == MeshPairingState.CLAIMED.value
    approved = await a_service.approve(pair_id)
    assert approved["epoch"] == 1
    return a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id


@pytest.mark.parametrize("mutation", ["term", "token", "pointer", "commit"])
async def test_export_snapshot_authority_change_blocks_final_ack(mutation: str) -> None:
    (
        a_service,
        b_service,
        _a_config,
        b_config,
        a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(67, 68)
    store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]

    if mutation == "term":
        await store.bump_term(3, updated_by_node_id="sourcenode0000000000000000000000")
    elif mutation == "token":
        await store.set_token(
            TokenLeaseState(
                state=TokenState.FREE,
                term=2,
                fencing_token=1,
                membership_epoch=2,
                bank_version=1,
            )
        )
    elif mutation == "pointer":
        await store.append_commit(
            BankCommit(
                bank_version=2,
                parent_bank_version=1,
                term=2,
                commit_id="c2",
                committed_by_node_id="sourcenode0000000000000000000000",
            )
        )
        await store.set_bank_version_pointer(
            BankVersionPointer(bank_version=2, commit_id="c2")
        )
    else:
        commit = await store.get_commit(1)
        assert commit is not None
        changed = commit.model_copy(update={"term": 3})
        await a_storage.put(
            layout.commit_key(SPACE, 1),
            json.dumps(changed.model_dump(mode="json")),
        )

    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    source_session = await a_service.store.get_session(pair_id)
    assert source_session is not None
    assert source_session.state == "blocked_recovery"
    evidence = await a_service.store.get_evidence(pair_id)
    assert evidence is not None
    assert evidence.evidence.phase == "bootstrap_source_changed"
    source_membership = await store.get_membership()
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    assert source_membership.epoch == 2
    assert any(
        member.node_id == target_node_id and member.status == "pending"
        for member in source_membership.members
    )
    target_health = await HivemindStateStore(
        storage=b_storage, space_id=SPACE
    ).get_node_status()  # type: ignore[arg-type]
    assert HiveNodeStatus(target_health.status) == HiveNodeStatus.UNSAFE
    assert await b_service.store.get_reservation(SPACE) == pair_id


async def test_prepared_source_health_and_provenance_loss_blocks_final_ack() -> None:
    """A prepared source cannot regain legacy no-marker compatibility at e+1."""

    (
        a_service,
        b_service,
        _a_config,
        b_config,
        a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_prepared_source_to_approved(69, 70)
    target = await b_service.store.get_session(pair_id)
    assert target is not None and target.state == MeshPairingState.CLAIMED.value

    # Import while the source's preparation/health proof is still exact, then
    # delete *both* records.  Looking only at live absence would incorrectly
    # reclassify this former prepared source as legacy and permit e+2.
    signed_env = await b_service._fetch_and_verify_approval(target)
    transferring = target.transition(
        MeshPairingState.APPROVED, now_ms=NOW_MS
    ).transition(
        MeshPairingState.TRANSFERRING,
        now_ms=NOW_MS,
        bootstrap_manifest_digest=signed_env.envelope.manifest_digest,
        bootstrap_bank_version=signed_env.envelope.bank_version,
    )
    await b_service.store.put_session(transferring)
    awaiting = await b_service._import_and_await(transferring, signed_env)
    await a_storage.delete(layout.node_status_key(SPACE))
    await a_storage.delete(a_service.store._source_preparation_key(SPACE))

    assert not await a_service._source_is_healthy_for_bootstrap(
        await a_service.store.get_session(pair_id)
    )
    assert not await a_service._source_health_marker_allows_bootstrap(
        await a_service.store.get_session(pair_id)
    )
    result = await b_service._final_ack_and_activate(pair_id, awaiting)

    assert result["ack_status"] != 200
    source_membership = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_membership()  # type: ignore[arg-type]
    assert source_membership is not None and source_membership.epoch == 1
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    assert any(
        member.node_id == target_node_id
        and member.status == "pending"
        for member in source_membership.members
    )
    target_health = await HivemindStateStore(
        storage=b_storage, space_id=SPACE
    ).get_node_status()  # type: ignore[arg-type]
    assert target_health is not None and target_health.status == HiveNodeStatus.UNSAFE.value
    assert await b_service.store.get_reservation(SPACE) == pair_id


async def test_export_snapshot_binds_signed_commit_content_not_only_commit_id() -> None:
    """A rewritten local evidence record cannot bless bytes different from the
    signed e+1 bootstrap, even when the selected commit id stays unchanged."""

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(68, 69)
    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    commit = await source_store.get_commit(1)
    assert commit is not None
    rewritten = commit.model_copy(update={"event_id": "same-id-rewritten"})
    await a_storage.put(
        layout.commit_key(SPACE, 1), json.dumps(rewritten.model_dump(mode="json"))
    )
    evidence = await a_service.store.get_source_bootstrap_evidence(pair_id)
    assert evidence is not None
    # Simulate a valid-schema corruption of the local operational record too.
    # The original source-signed envelope/payload must still reject this pair.
    tampered_evidence = replace(
        evidence.evidence,
        selected_commit_digest=a_service._canonical_model_digest(rewritten),
    )
    tampered = SignedSourceBootstrapEvidence.sign(
        tampered_evidence, a_service._config.private_key
    )
    await a_storage.put(
        a_service.store._source_bootstrap_evidence_key(pair_id),
        tampered.canonical_bytes().decode("utf-8"),
    )

    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    source_session = await a_service.store.get_session(pair_id)
    assert source_session is not None
    assert source_session.state == "blocked_recovery"
    blocked = await a_service.store.get_evidence(pair_id)
    assert blocked is not None
    assert blocked.evidence.phase == "bootstrap_source_changed"
    source_membership = await source_store.get_membership()
    assert source_membership is not None and source_membership.epoch == 2


async def test_pairing_authority_helpers_reject_missing_or_mutated_bindings() -> None:
    """All retained authority helpers fail closed instead of trusting sessions."""

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(68, 69)
    source = await a_service.store.get_session(pair_id)
    claimed = await b_service.store.get_session(pair_id)
    assert source is not None and claimed is not None

    # Both the wire caller and its persisted session must match the immutable
    # signed approval, not merely each other.
    request = SimpleNamespace(
        source_public_key=source.target_public_key,
        source_fingerprint=source.target_fingerprint,
    )
    assert await a_service._source_request_is_enrolled_target(source, request)
    request.source_fingerprint = "mesh:wrong"
    assert not await a_service._source_request_is_enrolled_target(source, request)

    source_approval = await a_service.store.get_blob(pair_id, "approval")
    assert source_approval is not None
    await a_storage.delete(a_service.store._blob_key(pair_id, "approval"))
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service._source_enrollment_approval(source)
    assert exc.value.code == "missing_artifacts"
    await a_service.store.put_blob(pair_id, "approval", source_approval)

    signed_env = await b_service._fetch_and_verify_approval(claimed)
    transferring = claimed.transition(
        MeshPairingState.APPROVED, now_ms=NOW_MS
    ).transition(
        MeshPairingState.TRANSFERRING,
        now_ms=NOW_MS,
        bootstrap_manifest_digest=signed_env.envelope.manifest_digest,
        bootstrap_bank_version=signed_env.envelope.bank_version,
    )
    await b_service.store.put_session(transferring)
    awaiting = await b_service._import_and_await(transferring, signed_env)
    assert (await b_service._target_enrollment_approval(awaiting)).pair_id == pair_id
    await b_storage.delete(b_service.store._blob_key(pair_id, "validated_approval"))
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service._target_enrollment_approval(awaiting)
    assert exc.value.code == "missing_artifacts"
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service._signed_pairing_binding(SimpleNamespace(role="invalid"))
    assert exc.value.code == "bad_binding"
    assert not await b_service._import_validation_matches(awaiting, base=1)

    # A source serving an explicit unsafe marker is not bootstrap-ready, and
    # neither a session-field substitution nor payload rewrite can pass the
    # retained signed snapshot checker.
    _signed, snapshot = await a_service._retained_bootstrap_snapshot(
        source,
        envelope_blob="bootstrap_envelope",
        payload_blob="bootstrap_payload",
    )
    missing_members = dict(snapshot.files)
    missing_members.pop("_hivemind/members.json")
    with pytest.raises(MeshPairingServiceError) as exc:
        a_service._bootstrap_snapshot_authority_models(
            SimpleNamespace(files=missing_members, manifest=snapshot.manifest),
            space_id=SPACE,
        )
    assert exc.value.code == "bootstrap_authority_mismatch"
    missing_commit = dict(snapshot.files)
    missing_commit.pop(
        layout.commit_key(SPACE, snapshot.manifest.bank_version)[len(SPACE) + 1 :]
    )
    with pytest.raises(MeshPairingServiceError) as exc:
        a_service._bootstrap_snapshot_authority_models(
            SimpleNamespace(files=missing_commit, manifest=snapshot.manifest),
            space_id=SPACE,
        )
    assert exc.value.code == "bootstrap_authority_mismatch"
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service._capture_source_bootstrap_evidence(
            source,
            membership_epoch=source.base_epoch + 99,
            manifest_digest=source.bootstrap_manifest_digest,
            bank_version=source.bootstrap_bank_version,
            commit_id="c1",
            recorded_at_ms=NOW_MS,
        )
    assert exc.value.code == "source_snapshot_changed"
    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    await source_store.set_node_status(
        NodeHealth(status=HiveNodeStatus.UNSAFE, reason="test")
    )
    assert not await a_service._source_is_healthy_for_bootstrap(source)
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service._retained_bootstrap_snapshot(
            source.with_fields(
                now_ms=source.updated_at_ms + 1,
                target_fingerprint=generate_mesh_identity().fingerprint
            ),
            envelope_blob="bootstrap_envelope",
            payload_blob="bootstrap_payload",
    )
    assert exc.value.code == "bootstrap_authority_mismatch"
    await a_service.store.put_blob(pair_id, "bootstrap_payload", b"tampered")
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service._retained_bootstrap_snapshot(
            source,
            envelope_blob="bootstrap_envelope",
            payload_blob="bootstrap_payload",
        )
    assert exc.value.code == "bootstrap_authority_mismatch"
    assert not await a_service._source_bootstrap_evidence_matches(source)


@pytest.mark.parametrize("mutation", ["missing", "tampered", "commit_rewrite"])
async def test_target_import_authority_is_required_at_activation(mutation: str) -> None:
    (
        _a_service,
        b_service,
        _a_config,
        _b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(69, 70)
    real_final_ack = b_service._final_ack_and_activate

    async def strip_or_tamper_marker(pair: str, session):
        key = b_service.store._import_validation_key(pair)
        if mutation == "missing":
            await b_storage.delete(key)
        elif mutation == "tampered":
            authority = await b_service.store.get_import_validation(pair)
            assert authority is not None
            tampered = replace(authority, manifest_digest="f" * 64)
            await b_storage.put(key, tampered.canonical_bytes().decode("utf-8"))
        else:
            target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
            commit = await target_store.get_commit(1)
            assert commit is not None
            rewritten = commit.model_copy(update={"event_id": "same-id-rewritten"})
            await b_storage.put(
                layout.commit_key(SPACE, 1),
                json.dumps(rewritten.model_dump(mode="json")),
            )
        return await real_final_ack(pair, session)

    b_service._final_ack_and_activate = strip_or_tamper_marker  # type: ignore[method-assign]
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    target_membership = await target_store.get_membership()
    target_node_id = (await target_store.get_node_identity()).node_id
    assert target_membership.epoch == 2
    assert any(
        member.node_id == target_node_id and member.status == "pending"
        for member in target_membership.members
    )
    target_health = await target_store.get_node_status()
    assert HiveNodeStatus(target_health.status) == HiveNodeStatus.UNSAFE
    assert await b_service.store.get_reservation(SPACE) == pair_id


async def test_import_authority_readback_rejects_conflict_pointer_and_corruption() -> None:
    (
        _a_service,
        b_service,
        _a_config,
        _b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(71, 72)
    claimed = await b_service.store.get_session(pair_id)
    assert claimed is not None and claimed.state == "claimed"
    signed_env = await b_service._fetch_and_verify_approval(claimed)
    transferring = claimed.transition(
        MeshPairingState.APPROVED, now_ms=NOW_MS
    ).transition(
        MeshPairingState.TRANSFERRING,
        now_ms=NOW_MS,
        bootstrap_manifest_digest=signed_env.envelope.manifest_digest,
        bootstrap_bank_version=signed_env.envelope.bank_version,
    )
    await b_service.store.put_session(transferring)
    awaiting = await b_service._import_and_await(transferring, signed_env)
    assert await b_service._import_validation_matches(awaiting, base=1)

    imported = SimpleNamespace(
        target_space_id=SPACE,
        local_node_id=b_service._config.fingerprint.split(":", 1)[1],
        membership_epoch=2,
        bank_version=1,
        commit_id="c1",
    )
    # The exact retry retains the original readback authority.
    await b_service._persist_import_validation(awaiting, signed_env, imported)
    authority = await b_service.store.get_import_validation(pair_id)
    assert authority is not None
    marker_key = b_service.store._import_validation_key(pair_id)
    original_marker = authority.canonical_bytes().decode("utf-8")

    await b_storage.put(
        marker_key,
        replace(authority, manifest_digest="f" * 64).canonical_bytes().decode("utf-8"),
    )
    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service._persist_import_validation(awaiting, signed_env, imported)
    assert exc.value.code == "import_validation_failed"
    await b_storage.put(marker_key, original_marker)

    target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    selected = await target_store.get_commit(1)
    assert selected is not None
    rewritten = selected.model_copy(update={"event_id": "same-id-rewritten"})
    await b_storage.put(
        layout.commit_key(SPACE, 1), json.dumps(rewritten.model_dump(mode="json"))
    )
    assert not await b_service._import_validation_matches(awaiting, base=1)
    # Restore the imported bytes before independently proving pointer drift.
    await b_storage.put(
        layout.commit_key(SPACE, 1), json.dumps(selected.model_dump(mode="json"))
    )
    await target_store.append_commit(
        BankCommit(
            bank_version=2,
            parent_bank_version=1,
            term=2,
            commit_id="c2",
            committed_by_node_id="sourcenode0000000000000000000000",
        )
    )
    await target_store.set_bank_version_pointer(
        BankVersionPointer(bank_version=2, commit_id="c2")
    )
    assert not await b_service._import_validation_matches(awaiting, base=1)
    await b_storage.put(marker_key, "{")
    assert not await b_service._import_validation_matches(awaiting, base=1)


async def test_source_rejects_unconfirmed_activation_response(monkeypatch):
    # The source must NOT mark the target active on an unsigned/unverifiable 2xx
    # (a misrouted/hostile endpoint returning 200 is not activation proof).
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(71, 72)

    class _Unsigned200:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(200, [], b"not-a-signed-response")

    a_service._sender_factory = lambda _e: _Unsigned200()  # type: ignore[attr-defined]
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    a_blocked = await a_service.store.get_session(pair_id)
    assert a_blocked.state == "blocked_recovery"  # not active on an unverified 200
    assert (await a_service.store.get_evidence(pair_id)) is not None


async def test_corrupt_bootstrap_import_blocks_target_recovery(monkeypatch):
    # A tampered bootstrap payload fails import; since the source already admitted
    # us PENDING, the target enters blocked_recovery with signed evidence (never a
    # pre-mutation cancel that would strand the source's pending member).
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(73, 74)

    class _CorruptBootstrap(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            resp = await super().send(method, path, headers=headers, body=body)
            if path.endswith("/bootstrap"):
                return PeerResponse(resp.status_code, resp.headers, resp.body + b"x")  # corrupt payload
            return resp

    b_service._sender_factory = lambda _e: _CorruptBootstrap(peers, "A")  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        await b_service.run_target_enrollment(pair_id)
    b_session = await b_service.store.get_session(pair_id)
    assert b_session.state == "blocked_recovery"  # not 'approved'/cancellable
    signed_ev = await b_service.store.get_evidence(pair_id)
    assert signed_ev is not None and signed_ev.evidence.next_action == "resync"


async def test_resync_rejects_mismatched_prefetched_payload_before_target_teardown() -> None:
    """A validly signed wire reply is insufficient if it differs from e+1.

    The resync preflight must verify the retained source envelope's payload
    commitment before it turns the target unsafe or deletes its recoverable
    state.  This simulates storage tampering of the source bootstrap blob while
    its pair route still signs the transport response normally.
    """

    a_service, b_service, _a_config, _b_config, a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(74, 75)
    )
    corrupt = {"on": True}

    class _CorruptFirstImport(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            response = await super().send(method, path, headers=headers, body=body)
            if corrupt["on"] and path.endswith("/bootstrap"):
                return PeerResponse(response.status_code, response.headers, response.body + b"x")
            return response

    b_service._sender_factory = lambda _endpoint: _CorruptFirstImport(peers, "A")  # type: ignore[attr-defined]
    with pytest.raises(MeshPairingServiceError):
        await b_service.run_target_enrollment(pair_id)
    assert (await b_service.store.get_session(pair_id)).state == "blocked_recovery"

    corrupt["on"] = False
    # Simulate an out-of-band rewrite after the source committed its signed
    # envelope.  The source signs the route response, but that response must
    # still be rejected because it is not the committed bootstrap bytes.
    payload_key = a_service.store._blob_key(pair_id, "bootstrap_payload")
    await a_storage.put(
        payload_key,
        base64.urlsafe_b64encode(b'{"not":"the-signed-bootstrap"}').decode("ascii"),
    )
    before = b_storage.snapshot()

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.resync(pair_id)

    assert exc.value.code == "bootstrap_mismatch"
    assert b_storage.snapshot() == before
    assert (await b_service.store.get_session(pair_id)).state == "blocked_recovery"
    assert await b_service.store.get_reservation(SPACE) == pair_id


async def test_resync_source_health_flip_before_bootstrap_keeps_target_unchanged() -> None:
    """A status->bootstrap health race cannot tear down the target first."""

    a_service, b_service, _a_config, _b_config, a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(75, 76)
    )
    corrupt = {"on": True}

    class _CorruptFirstImport(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            response = await super().send(method, path, headers=headers, body=body)
            if corrupt["on"] and path.endswith("/bootstrap"):
                return PeerResponse(response.status_code, response.headers, response.body + b"x")
            return response

    b_service._sender_factory = lambda _endpoint: _CorruptFirstImport(peers, "A")  # type: ignore[attr-defined]
    with pytest.raises(MeshPairingServiceError):
        await b_service.run_target_enrollment(pair_id)
    assert (await b_service.store.get_session(pair_id)).state == "blocked_recovery"

    corrupt["on"] = False
    flipped = {"done": False}

    class _FlipSourceUnsafeAfterStatus(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            response = await super().send(method, path, headers=headers, body=body)
            if not flipped["done"] and path.endswith("/status"):
                flipped["done"] = True
                await HivemindStateStore(storage=a_storage, space_id=SPACE).set_node_status(  # type: ignore[arg-type]
                    NodeHealth(status=HiveNodeStatus.UNSAFE, reason="test_source_flip")
                )
            return response

    b_service._sender_factory = lambda _endpoint: _FlipSourceUnsafeAfterStatus(peers, "A")  # type: ignore[attr-defined]
    before = b_storage.snapshot()

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.resync(pair_id)

    assert flipped["done"] is True
    assert exc.value.code == "no_bootstrap"
    assert b_storage.snapshot() == before
    assert (await b_service.store.get_session(pair_id)).state == "blocked_recovery"
    assert await b_service.store.get_reservation(SPACE) == pair_id


async def test_resync_post_marker_failure_cannot_overwrite_raced_active_target() -> None:
    """A late resync failure re-reads the target receipt before blocking it.

    The source can resume and deliver e+2 after the replacement marker is
    durable but before the resync worker completes its readback.  That worker
    must not subsequently replace a just-written ACTIVE session with stale
    ``blocked_recovery`` bookkeeping.
    """

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(76, 77)
    )

    class _BlackHole:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(503, [], b"")

    a_service._sender_factory = lambda _endpoint: _BlackHole()  # type: ignore[attr-defined]
    initial = await b_service.run_target_enrollment(pair_id)
    assert initial["state"] == MeshPairingState.AWAITING_ACKS.value
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"
    a_service._sender_factory = lambda _endpoint: AsgiPeerSender(peers, "B")  # type: ignore[attr-defined]

    # Force the target through its evidence-gated resync path.
    await b_storage.delete(b_service.store._import_validation_key(pair_id))
    real_persist = b_service._persist_import_validation
    marker_written = asyncio.Event()
    release_failure = asyncio.Event()

    async def persist_then_fail(*args, **kwargs):
        await real_persist(*args, **kwargs)
        marker_written.set()
        await release_failure.wait()
        raise RuntimeError("simulated post-marker readback failure")

    b_service._persist_import_validation = persist_then_fail  # type: ignore[method-assign]
    resync_task = asyncio.create_task(b_service.resync(pair_id))
    await asyncio.wait_for(marker_written.wait(), timeout=ASYNC_RACE_TIMEOUT_SECONDS)

    # The fresh marker permits the source's existing e+2 resume to complete the
    # target activation concurrently with the stale resync worker.
    resumed = await a_service.resume(pair_id)
    assert resumed["state"] == "active"
    release_failure.set()
    result = await asyncio.wait_for(resync_task, timeout=ASYNC_RACE_TIMEOUT_SECONDS)
    b_service._persist_import_validation = real_persist  # type: ignore[method-assign]

    assert result["state"] == "active"
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    assert await b_service.store.get_reservation(SPACE) is None


async def test_resync_and_stale_activation_delivery_linearize_at_target_tail() -> None:
    """A stale e+2 cannot finalize after resync begins destructive recovery."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers, pair_id = (
        await _drive_to_approved(151, 152)
    )
    # Leave e+2 applied but make the terminal receipt unavailable.  This is
    # intentionally distinct from a HEALTHY-tail crash: a valid signed receipt
    # must now win over a stale resync rather than tearing the target down.
    real_persist_receipt = b_service._persist_target_activation_receipt
    fail_receipt = {"on": True}

    async def fail_first_receipt(session, *, base):
        if fail_receipt["on"]:
            fail_receipt["on"] = False
            raise RuntimeError("simulated terminal receipt write failure")
        return await real_persist_receipt(session, base=base)

    b_service._persist_target_activation_receipt = fail_first_receipt  # type: ignore[method-assign]
    await b_service.run_target_enrollment(pair_id)
    b_service._persist_target_activation_receipt = real_persist_receipt  # type: ignore[method-assign]
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"
    assert (await b_service.store.get_session(pair_id)).state == "awaiting_acks"
    assert await b_service.store.get_target_activation_receipt(pair_id) is None

    real_matches = b_service._import_validation_matches
    match_count = 0
    finalizer_precheck = asyncio.Event()
    release_finalizer = asyncio.Event()

    async def pause_stale_finalizer(session, *, base):
        nonlocal match_count
        matched = await real_matches(session, base=base)
        match_count += 1
        if match_count == 2:
            finalizer_precheck.set()
            await release_finalizer.wait()
        return matched

    b_service._import_validation_matches = pause_stale_finalizer  # type: ignore[method-assign]
    resume_task = asyncio.create_task(a_service.resume(pair_id))
    await asyncio.wait_for(finalizer_precheck.wait(), timeout=ASYNC_RACE_TIMEOUT_SECONDS)

    # The target begins an evidence-gated resync while the old delivery is
    # paused after its pre-lock marker check.  The resync lock must prevent that
    # stale delivery from releasing the reservation or writing ACTIVE over its
    # replacement import state.
    await b_storage.delete(b_service.store._import_validation_key(pair_id))
    real_teardown = b_service._teardown_target_space
    teardown_entered = asyncio.Event()
    release_teardown = asyncio.Event()

    async def pause_teardown(space_id):
        teardown_entered.set()
        await release_teardown.wait()
        await real_teardown(space_id)

    b_service._teardown_target_space = pause_teardown  # type: ignore[method-assign]
    resync_task = asyncio.create_task(b_service.resync(pair_id))
    # The stale finalizer already owns the same space-tail lock.  Releasing it
    # first lets its second marker check observe the deletion and refuse, then
    # the resync worker acquires the lock and performs the destructive reset.
    # Waiting for teardown while that finalizer remains paused would manufacture
    # a test-only lock inversion rather than exercise a protocol race.
    release_finalizer.set()
    await asyncio.wait_for(teardown_entered.wait(), timeout=ASYNC_RACE_TIMEOUT_SECONDS)
    release_teardown.set()

    await asyncio.wait_for(resume_task, timeout=ASYNC_RACE_TIMEOUT_SECONDS)
    result = await asyncio.wait_for(resync_task, timeout=ASYNC_RACE_TIMEOUT_SECONDS)
    b_service._import_validation_matches = real_matches  # type: ignore[method-assign]
    b_service._teardown_target_space = real_teardown  # type: ignore[method-assign]

    assert result["state"] == "active"
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    health = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert health is not None and HiveNodeStatus(health.status) == HiveNodeStatus.HEALTHY
    assert await b_service.store.get_reservation(SPACE) is None


async def test_crash_window_A_source_crash_before_persisting_active_converges(monkeypatch):
    # Crash the SOURCE right AFTER it verifies the target's activation confirmation
    # but BEFORE it persists its own ACTIVE session. The target is fully active
    # (HEALTHY), so on resume the source re-delivers e+2 to a healthy target: the
    # idempotent handle_event reconfirmation must return the SAME signed active
    # confirmation (not a generic ack), so BOTH converge — never a live-member evict.
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(81, 82)
    real_put = a_service.store.put_session

    async def crash_on_source_active(session):
        if session.role == "source" and session.state == "active":
            raise RuntimeError("simulated crash before persisting source active")
        return await real_put(session)

    a_service.store.put_session = crash_on_source_active  # type: ignore[method-assign]
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] == "active"  # target self-activated + healthy
    a_service.store.put_session = real_put  # type: ignore[method-assign]  # "restart"
    assert (await a_service.store.get_session(pair_id)).state == "awaiting_acks"  # stranded

    resumed = await a_service.resume(pair_id)
    assert resumed["state"] == "active"
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"


async def test_crash_window_A_marker_loss_uses_signed_terminal_receipt() -> None:
    """Marker loss after e+2 can recover only through the signed terminal proof."""

    a_service, b_service, _a_config, b_config, _a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(82, 83)
    )
    real_put = a_service.store.put_session

    async def crash_on_source_active(session):
        if session.role == "source" and session.state == "active":
            raise RuntimeError("simulated crash before persisting source active")
        return await real_put(session)

    a_service.store.put_session = crash_on_source_active  # type: ignore[method-assign]
    initial = await b_service.run_target_enrollment(pair_id)
    assert initial["state"] == "active"
    a_service.store.put_session = real_put  # type: ignore[method-assign]
    assert (await a_service.store.get_session(pair_id)).state == "awaiting_acks"
    assert (await b_service.store.get_session(pair_id)).state == "active"

    await b_storage.delete(b_service.store._import_validation_key(pair_id))
    resumed = await a_service.resume(pair_id)

    assert resumed["state"] == "active"
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    assert await b_service.store.get_import_validation(pair_id) is None
    receipt = await b_service.store.get_target_activation_receipt(pair_id)
    assert receipt is not None
    receipt.verify(b_config.public_key)


async def test_terminal_receipt_rejects_post_e2_head_rewrite_after_marker_loss() -> None:
    """A local valid-schema BANK_COMMIT cannot replace e+1 import authority.

    The source has applied e+2 but crashed before its own ACTIVE receipt.  If
    the target subsequently loses the import marker, a forged pointer/commit
    chain must not be accepted merely because its parent and manifest are
    locally coherent: no all-ACK authorization binds that new head to the
    retained snapshot.
    """

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(83, 84)
    real_put = a_service.store.put_session

    async def crash_on_source_active(session):
        if session.role == "source" and session.state == "active":
            raise RuntimeError("simulated crash before persisting source active")
        return await real_put(session)

    a_service.store.put_session = crash_on_source_active  # type: ignore[method-assign]
    initial = await b_service.run_target_enrollment(pair_id)
    a_service.store.put_session = real_put  # type: ignore[method-assign]
    assert initial["state"] == MeshPairingState.ACTIVE.value
    await b_storage.delete(b_service.store._import_validation_key(pair_id))

    target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await target_store.get_membership()
    term = await target_store.get_term()
    assert membership is not None and term is not None and membership.epoch == 3
    source_member = next(
        member for member in membership.members if member.node_id != b_service._config.fingerprint.split(":", 1)[1]
    )
    forged = BankCommit(
        bank_version=2,
        parent_bank_version=1,
        term=term.term,
        membership_epoch=membership.epoch,
        commit_id="forged-valid-schema-head",
        committed_by_node_id=source_member.node_id,
        manifest=[],
    )
    await target_store.append_commit(forged)
    await target_store.set_bank_version_pointer(
        BankVersionPointer(bank_version=forged.bank_version, commit_id=forged.commit_id)
    )

    resumed = await a_service.resume(pair_id)

    assert resumed["state"] == MeshPairingState.BLOCKED_RECOVERY.value
    assert (await a_service.store.get_session(pair_id)).state == MeshPairingState.BLOCKED_RECOVERY.value
    target_health = await target_store.get_node_status()
    assert target_health is not None and target_health.status == HiveNodeStatus.UNSAFE.value
    assert await b_service.store.get_reservation(SPACE) == pair_id
    pointer = await target_store.get_bank_version_pointer()
    assert pointer is not None and pointer.commit_id == forged.commit_id


async def test_both_target_terminal_authorities_recover_only_from_source_e2() -> None:
    """Loss of marker+receipt fences a live target until its source confirms e+2."""

    a_service, b_service, _a_config, b_config, _a_storage, b_storage, _peers, pair_id = (
        await _drive_to_approved(82, 83)
    )
    real_put = a_service.store.put_session

    async def crash_on_source_active(session):
        if session.role == "source" and session.state == "active":
            raise RuntimeError("simulated crash before persisting source active")
        return await real_put(session)

    a_service.store.put_session = crash_on_source_active  # type: ignore[method-assign]
    initial = await b_service.run_target_enrollment(pair_id)
    a_service.store.put_session = real_put  # type: ignore[method-assign]
    assert initial["state"] == MeshPairingState.ACTIVE.value
    assert (await a_service.store.get_session(pair_id)).state == "awaiting_acks"
    assert (await b_service.store.get_session(pair_id)).state == "active"

    # The target's active workflow record is mutable operational state.  Losing
    # both durable authorities cannot leave it HEALTHY or self-repair locally.
    await b_storage.delete(b_service.store._import_validation_key(pair_id))
    await b_storage.delete(b_service.store._target_activation_receipt_key(pair_id))

    recovered = await b_service.run_target_enrollment(pair_id)

    assert recovered["ack_status"] == 200
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    health = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert health is not None and HiveNodeStatus(health.status) == HiveNodeStatus.HEALTHY
    assert await b_service.store.get_reservation(SPACE) is None
    receipt = await b_service.store.get_target_activation_receipt(pair_id)
    assert receipt is not None
    receipt.verify(b_config.public_key)


async def test_crash_window_B_target_crash_before_healthy_converges(monkeypatch):
    # Crash the TARGET after apply_self_activation (membership -> e+2) but BEFORE the
    # finalize tail (HEALTHY + reservation release + session -> active). On resume
    # the target is still UNSAFE, so re-delivery routes back through the confined
    # branch, whose idempotent path must RE-RUN the finalize tail so the target
    # converges (HEALTHY + released + active) and the source confirms.
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(83, 84)
    real_finalize = b_service._finalize_target_activation
    calls = {"n": 0}

    async def crash_first(session, *, base):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("crash after apply_self_activation, before HEALTHY")
        return await real_finalize(session, base=base)

    b_service._finalize_target_activation = crash_first  # type: ignore[method-assign]
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"  # finalize crashed
    # Target: membership promoted but node still UNSAFE, reservation still held.
    b_health = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert HiveNodeStatus(b_health.status) == HiveNodeStatus.UNSAFE
    assert await b_service.store.get_reservation(SPACE) == pair_id
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"

    b_service._finalize_target_activation = real_finalize  # type: ignore[method-assign]  # "restart"
    resumed = await a_service.resume(pair_id)
    assert resumed["state"] == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    b_health2 = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert HiveNodeStatus(b_health2.status) == HiveNodeStatus.HEALTHY
    assert await b_service.store.get_reservation(SPACE) is None  # released on convergence


async def test_target_terminal_receipt_recovers_marker_loss_before_healthy() -> None:
    """A signed e+2 receipt heals the crash tail after marker loss.

    The receipt is deliberately persisted after exact e+2 proof but before
    HEALTHY/session/release.  A failure at that boundary followed by marker
    loss must remain resumable without treating the mutable target session as
    activation authority.
    """

    a_service, b_service, _a_config, b_config, _a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(84, 85)
    )
    real_put = b_storage.put
    fail = {"on": True}

    async def fail_healthy_status(key, content, content_type="text/plain"):
        if (
            fail["on"]
            and key == layout.node_status_key(SPACE)
            and '"healthy"' in content
        ):
            raise OSError("simulated healthy-status write failure")
        await real_put(key, content, content_type)

    b_storage.put = fail_healthy_status  # type: ignore[method-assign]
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    target = await b_service.store.get_session(pair_id)
    assert target is not None and target.state != MeshPairingState.ACTIVE.value
    health = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert health is not None and HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE
    assert await b_service.store.get_reservation(SPACE) == pair_id
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"
    receipt = await b_service.store.get_target_activation_receipt(pair_id)
    assert receipt is not None
    receipt.verify(b_config.public_key)

    fail["on"] = False
    b_storage.put = real_put  # type: ignore[method-assign]
    # Simulate a restart after an independent marker-loss/corruption event.
    # The signed terminal receipt, retained snapshot, and exact e+2 view are
    # sufficient; neither a fresh marker nor a mutable ACTIVE session is.
    await b_service.store.clear_import_validation_for_resync(pair_id)
    a_service._clock_ms = lambda: NOW_MS + 1_000  # type: ignore[method-assign]
    b_service._clock_ms = lambda: NOW_MS + 1_000  # type: ignore[method-assign]
    resumed = await a_service.resume(pair_id)
    assert resumed["state"] == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    assert await b_service.store.get_reservation(SPACE) is None


async def test_resync_replaces_rejected_terminal_receipt_after_marker_loss() -> None:
    """A malformed receipt cannot permanently conflict with a fresh import."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(85, 86)
    )

    class _BlackHole:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(503, [], b"")

    # The source has e+2 but cannot deliver it, leaving the target at its
    # imported e+1 marker.  A damaged terminal-receipt object plus marker loss
    # must take the evidence-gated destructive resync route, which clears both
    # rejected authorities immediately before the fresh signed import.
    a_service._sender_factory = lambda _endpoint: _BlackHole()  # type: ignore[method-assign]
    initial = await b_service.run_target_enrollment(pair_id)
    assert initial["state"] == MeshPairingState.AWAITING_ACKS.value
    await b_service.store.clear_import_validation_for_resync(pair_id)
    await b_storage.put(
        b_service.store._target_activation_receipt_key(pair_id), "{"
    )
    a_service._sender_factory = lambda _endpoint: AsgiPeerSender(peers, "B")  # type: ignore[method-assign]

    repaired = await b_service.resync(pair_id)

    assert repaired["state"] == MeshPairingState.ACTIVE.value
    assert repaired["ack_status"] == 200
    receipt = await b_service.store.get_target_activation_receipt(pair_id)
    assert receipt is not None
    assert await b_service.store.get_reservation(SPACE) is None


async def test_source_terminal_receipt_holds_target_write_fence_until_ack() -> None:
    """A target e+2 receipt is not the final all-ACK boundary.

    If the target crashes/fails while releasing its final reservation, the
    source is already durably ACTIVE but keeps its source activation fence.  No
    ordinary target write may slip into that interval: an authenticated retry
    of the source-signed terminal receipt is the only operation that releases
    both sides' fences.
    """

    from live_mem.core.reservation_guard import (
        PairingActivationError,
        assert_space_not_reserved,
        clear_reservation_checker,
        register_reservation_checker,
    )
    from live_mem.mesh.pairing_store import MeshPairingStoreError

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, _peers, pair_id = (
        await _drive_to_approved(85, 86)
    )
    real_release = b_service.store.release
    fail_release = {"on": True}

    async def fail_first_target_release(space_id, released_pair_id):
        if (
            fail_release["on"]
            and space_id == SPACE
            and released_pair_id == pair_id
        ):
            fail_release["on"] = False
            raise OSError("simulated target reservation release failure")
        await real_release(space_id, released_pair_id)

    b_service.store.release = fail_first_target_release  # type: ignore[method-assign]
    initial = await b_service.run_target_enrollment(pair_id)
    b_service.store.release = real_release  # type: ignore[method-assign]
    assert initial["state"] == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    assert await b_service.store.get_reservation(SPACE) == pair_id
    # The source has durably reached ACTIVE, but its terminal source receipt
    # remains fenced until the target can persist/release its matching tail.
    assert (await a_service.store.get_session(pair_id)).state == "active"
    with pytest.raises(PairingActivationError):
        await a_service.assert_no_pairing_activation(SPACE)

    # The target is e+2/HEALTHY but must remain write-fenced until it has
    # read-back verified the source-signed receipt.  This is the regression
    # boundary for the former source-crash -> post-e2 BANK_COMMIT gap.
    register_reservation_checker(b_service.assert_space_not_reserved)
    try:
        with pytest.raises(MeshPairingStoreError) as exc:
            await assert_space_not_reserved(SPACE)
        assert exc.value.code == "space_reserved"
    finally:
        clear_reservation_checker()

    # A source restart/retry does not replay e+2 as authority.  It replays its
    # exact signed terminal receipt and the target then releases only its own
    # matching reservation.
    repaired = await a_service.resume(pair_id)

    assert repaired["state"] == "active"
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert await b_service.store.get_reservation(SPACE) is None
    await a_service.assert_no_pairing_activation(SPACE)

    register_reservation_checker(b_service.store.assert_space_not_reserved)
    try:
        await assert_space_not_reserved(SPACE)
    finally:
        clear_reservation_checker()


async def test_source_terminal_retry_reconciles_timestamp_variant_receipt() -> None:
    """A target-confirmed receipt canonicalizes a restarted source retry.

    ``confirmed_at_ms`` is observational.  When the source loses its local
    receipt after the target has retained an otherwise identical copy, a later
    retry must adopt the target-confirmed bytes rather than strand the final
    all-ACK digest on two timestamp-only variants.
    """

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(227, 228)
    real_put_terminal = b_service.store.put_target_terminal_confirmation
    fail = {"on": True}

    async def fail_first_terminal_confirmation(signed):
        if fail["on"]:
            fail["on"] = False
            raise OSError("simulated target terminal-confirmation failure")
        await real_put_terminal(signed)

    b_service.store.put_target_terminal_confirmation = fail_first_terminal_confirmation  # type: ignore[method-assign]
    initial = await b_service.run_target_enrollment(pair_id)
    b_service.store.put_target_terminal_confirmation = real_put_terminal  # type: ignore[method-assign]
    assert initial["ack_status"] != 200
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert await b_service.store.get_reservation(SPACE) == pair_id

    retained_at_target = await b_service.store.get_source_activation_receipt(pair_id)
    assert retained_at_target is not None
    await a_storage.delete(a_service.store._source_activation_receipt_key(pair_id))
    a_service._clock_ms = lambda: NOW_MS + 1  # type: ignore[method-assign]

    # The first retry may create a new timestamp-only observation before B
    # returns its retained terminal chain; it must remain safely pending.
    first = await a_service.resume(pair_id)
    assert first["state"] == "active"
    restored_at_source = await a_service.store.get_source_activation_receipt(pair_id)
    assert restored_at_source is not None

    # Replaying the target-confirmed receipt now converges the terminal chain.
    repaired = await a_service.resume(pair_id)
    assert repaired["state"] == "active"
    assert repaired.get("source_confirmation_pending") is not True
    assert (await b_service.store.get_session(pair_id)).state == "active"
    assert await b_service.store.get_reservation(SPACE) is None
    source_after = await a_service.store.get_source_activation_receipt(pair_id)
    target_after = await b_service.store.get_source_activation_receipt(pair_id)
    source_terminal = await a_service.store.get_target_terminal_confirmation(pair_id)
    target_terminal = await b_service.store.get_target_terminal_confirmation(pair_id)
    assert source_after is not None and target_after is not None
    assert source_terminal is not None and target_terminal is not None
    assert source_after.canonical_bytes() == target_after.canonical_bytes()
    assert source_terminal.canonical_bytes() == target_terminal.canonical_bytes()


async def test_terminal_confirmation_payloads_require_exact_signed_bindings() -> None:
    """Final-ACK replay helpers refuse incomplete or retargeted signed data.

    This exercises the source and target's independent parsers directly after
    a real all-ACK pairing.  A response body is an untrusted transport
    projection: it may only hydrate the exact receipt/digest pair already
    bound by the invitation, source activation event, and target signature.
    """

    import hashlib

    from live_mem.mesh.pairing_state import (
        SignedTargetTerminalConfirmationReceipt,
    )

    (
        a_service,
        b_service,
        _a_config,
        b_config,
        a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(238, 239)
    activated = await b_service.run_target_enrollment(pair_id)
    assert activated["state"] == MeshPairingState.ACTIVE.value
    source_session = await a_service.store.get_session(pair_id)
    target_session = await b_service.store.get_session(pair_id)
    target_receipt = await b_service.store.get_target_activation_receipt(pair_id)
    source_receipt = await a_service.store.get_source_activation_receipt(pair_id)
    terminal = await b_service.store.get_target_terminal_confirmation(pair_id)
    event = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_event(source_session.activation_event_id)  # type: ignore[arg-type]
    assert (
        source_session is not None
        and target_session is not None
        and target_receipt is not None
        and source_receipt is not None
        and terminal is not None
        and event is not None
    )

    confirmation = {
        "target_activation_receipt": target_receipt.as_dict(),
        "target_activation_receipt_digest": hashlib.sha256(
            target_receipt.canonical_bytes()
        ).hexdigest(),
        "target_terminal_confirmation": terminal.as_dict(),
    }
    replay_event = a_service._event_with_source_activation_receipt(
        event,
        source_receipt,
        "terminal-binding-replay",
        terminal_confirmation=terminal,
    )
    assert await a_service._target_receipt_from_activation_confirmation(
        source_session, confirmation, base=source_session.base_epoch
    ) == target_receipt
    assert await b_service._target_terminal_confirmation_for_event(
        target_session,
        source_receipt,
        replay_event,
        base=target_session.base_epoch,
    ) == terminal
    assert await a_service._persist_target_terminal_confirmation(
        source_session, source_receipt, confirmation
    )

    # A missing or digest-mismatched target proof cannot be promoted into a
    # durable terminal acknowledgement by either side.
    assert await a_service._target_receipt_from_activation_confirmation(
        source_session, {}, base=source_session.base_epoch
    ) is None
    bad_digest = dict(confirmation)
    bad_digest["target_activation_receipt_digest"] = "0" * 64
    assert await a_service._target_receipt_from_activation_confirmation(
        source_session, bad_digest, base=source_session.base_epoch
    ) is None
    assert not await a_service._persist_target_terminal_confirmation(
        source_session, source_receipt, {}
    )
    assert await b_service._target_terminal_confirmation_for_event(
        target_session,
        source_receipt,
        replay_event,
        base=target_session.base_epoch,
    ) == terminal

    # Even a freshly valid target signature is not portable to another pair.
    # Its field binding is as important as signature verification.
    wrong_pair_terminal = SignedTargetTerminalConfirmationReceipt.sign(
        replace(terminal.receipt, pair_id="pair_" + "f" * 32),
        b_config.private_key,
    )
    retargeted = dict(confirmation)
    retargeted["target_terminal_confirmation"] = wrong_pair_terminal.as_dict()
    retargeted_event = a_service._event_with_source_activation_receipt(
        event,
        source_receipt,
        "retargeted-terminal-replay",
        terminal_confirmation=wrong_pair_terminal,
    )
    assert await b_service._target_terminal_confirmation_for_event(
        target_session,
        source_receipt,
        retargeted_event,
        base=target_session.base_epoch,
    ) is None
    assert not await a_service._persist_target_terminal_confirmation(
        source_session, source_receipt, retargeted
    )


@pytest.mark.parametrize(
    "mutation", ["event_epoch", "event_pair", "missing_evidence", "node_digest"]
)
async def test_source_terminal_receipt_requires_exact_e2_binding(
    mutation: str,
) -> None:
    """The source final-ACK signer rechecks every retained e+1/e+2 binding."""

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(240, 241)
    activated = await b_service.run_target_enrollment(pair_id)
    assert activated["state"] == MeshPairingState.ACTIVE.value
    source_session = await a_service.store.get_session(pair_id)
    assert source_session is not None
    state = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    event = await state.get_event(source_session.activation_event_id)
    membership = await state.get_membership()
    assert event is not None and membership is not None
    # The durable event keeps only the generic membership update.  The source
    # attaches this pairing-local binding when it sends the final e+2 delivery.
    from live_mem.mesh.membership_sync import candidate_view_digest

    event = event.model_copy(
        update={
            "payload": {
                **event.payload,
                "pair_id": pair_id,
                "candidate_view_digest": candidate_view_digest(membership),
            }
        }
    )
    assert await a_service._source_terminal_activation_state_matches(
        source_session, event
    )

    if mutation == "event_epoch":
        event = event.model_copy(
            update={"membership_epoch": source_session.base_epoch + 3}
        )
    elif mutation == "event_pair":
        event = event.model_copy(
            update={
                "payload": {
                    **event.payload,
                    "pair_id": "pair_" + "e" * 32,
                }
            }
        )
    elif mutation == "missing_evidence":
        await a_storage.delete(
            a_service.store._source_bootstrap_evidence_key(pair_id)
        )
    else:
        node = await state.get_node_identity()
        assert node is not None
        await state.set_node_identity(
            node.model_copy(update={"display_name": "unexpected-node"})
        )

    assert not await a_service._source_terminal_activation_state_matches(
        source_session, event
    )


@pytest.mark.parametrize("damage", ["evidence", "incarnation"])
async def test_source_terminal_marker_survives_lost_fence_provenance(damage: str) -> None:
    """A #417 source tail never falls back to legacy after mutable-loss damage."""

    from live_mem.core.reservation_guard import PairingActivationError

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(226, 227)
    real_put_terminal = b_service.store.put_target_terminal_confirmation
    fail = {"on": True}

    async def fail_terminal_confirmation(signed):
        if fail["on"]:
            fail["on"] = False
            raise OSError("simulated target terminal-confirmation crash")
        await real_put_terminal(signed)

    b_service.store.put_target_terminal_confirmation = fail_terminal_confirmation  # type: ignore[method-assign]
    result = await b_service.run_target_enrollment(pair_id)
    b_service.store.put_target_terminal_confirmation = real_put_terminal  # type: ignore[method-assign]
    assert result["ack_status"] != 200
    assert (await a_service.store.get_session(pair_id)).state == "active"

    # The primary binding and member incarnation are both mutable storage
    # projections.  The separately keyed source-tail marker must still fence
    # ordinary source mutations while the target has not signed final all-ACK.
    await a_storage.delete(a_service.store._activation_fence_key(SPACE))
    if damage == "evidence":
        await a_storage.delete(a_service.store._source_bootstrap_evidence_key(pair_id))
    else:
        source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
        membership = await source_store.get_membership()
        assert membership is not None
        target_id = b_service._config.fingerprint.split(":", 1)[1]
        await source_store.set_membership(
            membership.model_copy(
                update={
                    "members": [
                        member.model_copy(update={"incarnation": None})
                        if member.node_id == target_id
                        else member
                        for member in membership.members
                    ]
                }
            )
        )

    restarted = MeshPairingService(
        a_service._config,
        a_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=a_service._sender_factory,
    )
    with pytest.raises(PairingActivationError):
        await restarted.assert_no_pairing_activation(SPACE)


@pytest.mark.parametrize("damage", ["downgrade", "delete"])
async def test_source_protocol_floor_rejects_lost_or_downgraded_tail_index(
    damage: str,
) -> None:
    """A #417 source cannot fall back to legacy after losing tail records.

    The re-armed source index is signed, while a separate immutable protocol
    floor records that this source has entered #417.  Removing the primary
    evidence/marker/fence and then either downgrading that index to a valid v1
    legacy record or deleting it must still keep ordinary e+3 mutations fenced.
    """

    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(229, 230)
    real_put_terminal = b_service.store.put_target_terminal_confirmation
    fail = {"on": True}

    async def fail_terminal_confirmation(signed):
        if fail["on"]:
            fail["on"] = False
            raise OSError("simulated target terminal-confirmation crash")
        await real_put_terminal(signed)

    b_service.store.put_target_terminal_confirmation = fail_terminal_confirmation  # type: ignore[method-assign]
    result = await b_service.run_target_enrollment(pair_id)
    b_service.store.put_target_terminal_confirmation = real_put_terminal  # type: ignore[method-assign]
    assert result["ack_status"] != 200

    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await source_store.get_membership()
    assert membership is not None
    source_node_id = next(
        member.node_id
        for member in membership.members
        if member.node_id != b_service._config.fingerprint.split(":", 1)[1]
    )
    await a_storage.delete(a_service.store._source_bootstrap_evidence_key(pair_id))
    await a_storage.delete(a_service.store._source_activation_marker_key(SPACE))
    await a_storage.delete(a_service.store._activation_fence_key(SPACE))

    if damage == "downgrade":
        await a_storage.put(
            a_service.store._activation_migration_key(SPACE),
            canonical_dumps(
                {
                    "pair_id": pair_id,
                    "protocol_version": 1,
                    "space_id": SPACE,
                    "updated_at_ms": NOW_MS + 1,
                }
            ).decode("utf-8"),
        )
    else:
        await a_storage.delete(a_service.store._activation_migration_key(SPACE))

    restarted = MeshPairingService(
        a_service._config,
        a_storage,
        clock_ms=lambda: NOW_MS + 2,
        sender_factory=a_service._sender_factory,
    )
    register_pairing_activation_checker(restarted.assert_no_pairing_activation)
    try:
        with pytest.raises(PairingActivationError):
            await restarted._membership(SPACE).update_member_scopes(
                source_node_id, ["read"]
            )
    finally:
        clear_pairing_activation_checker()
    held = await source_store.get_membership()
    assert held is not None and held.epoch == membership.epoch


async def test_historical_source_marker_cannot_mask_a_new_unconfirmed_tail() -> None:
    """A valid marker from an evicted peer is not current tail authority.

    The per-space marker is intentionally signed, but signatures alone do not
    make an old snapshot current.  Replaying P0's completed marker while P1 is
    in its e+2 final-confirmation tail must not let the ordinary-write guard
    discard P1 merely because P0's historical session remains ACTIVE.
    """

    from live_mem.core.reservation_guard import PairingActivationError

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        peers,
        first_pair,
    ) = await _drive_to_approved(230, 231)
    stale_marker = await a_service.store.get_source_activation_marker(SPACE)
    assert stale_marker is not None
    assert (await b_service.run_target_enrollment(first_pair))["ack_status"] == 200
    await a_service.force_evict_member(first_pair, operator="operator", reason="dead")

    c_config = _config(
        MESH_PRIVATE_KEY_PREFIX
        + base64.urlsafe_b64encode(bytes([232]) * 32).decode().rstrip("="),
        "https://c.mesh.test",
    )
    c_storage = FakeStorage()
    await _seed_blank_target(c_storage)
    c_service = MeshPairingService(
        c_config,
        c_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=lambda _endpoint: AsgiPeerSender(peers, "A"),
    )
    peers["C"] = MeshNamespaceRouter(
        _fallback(),
        config=c_config,
        process_lock=FakeProcessLock(),
        storage_factory=lambda: c_storage,
        replay_ledger=FakeReplayLedger(),
        clock_ms=lambda: NOW_MS,
        pairing_service=c_service,
    )
    a_service._sender_factory = lambda endpoint: AsgiPeerSender(
        peers, "C" if urlsplit(endpoint).hostname == "c.mesh.test" else "B"
    )  # type: ignore[method-assign]

    invitation = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    second_pair = invitation["pair_id"]
    await c_service.accept_invitation(
        invitation["invitation_bytes"],
        SPACE,
        secret=invitation["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    await a_service.approve(second_pair)

    real_put_terminal = c_service.store.put_target_terminal_confirmation
    failed = {"once": True}

    async def fail_terminal_confirmation(signed):
        if failed["once"]:
            failed["once"] = False
            raise OSError("simulated target terminal-confirmation crash")
        await real_put_terminal(signed)

    c_service.store.put_target_terminal_confirmation = fail_terminal_confirmation  # type: ignore[method-assign]
    result = await c_service.run_target_enrollment(second_pair)
    c_service.store.put_target_terminal_confirmation = real_put_terminal  # type: ignore[method-assign]
    assert result["ack_status"] != 200
    assert (await a_service.store.get_session(second_pair)).state == "active"

    # Replace M1 with the still-valid but historical M0, then remove P1's two
    # mutable discovery paths.  Current membership is the monotonic authority
    # that rejects M0 instead of releasing it as a completed historical tail.
    await a_storage.put(
        a_service.store._source_activation_marker_key(SPACE),
        stale_marker.canonical_bytes().decode("utf-8"),
    )
    await a_storage.delete(a_service.store._activation_fence_key(SPACE))
    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await source_store.get_membership()
    assert membership is not None
    target_id = c_config.fingerprint.split(":", 1)[1]
    await source_store.set_membership(
        membership.model_copy(
            update={
                "members": [
                    member.model_copy(update={"incarnation": None})
                    if member.node_id == target_id
                    else member
                    for member in membership.members
                ]
            }
        )
    )

    restarted = MeshPairingService(
        a_service._config,
        a_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=a_service._sender_factory,
    )
    with pytest.raises(PairingActivationError):
        await restarted.assert_no_pairing_activation(SPACE)


async def test_lost_source_marker_cannot_unfence_current_signed_e2_tail() -> None:
    """Primary signed evidence fences a tail when mutable discovery data is lost.

    A delayed exact e+2 is still valid at the target.  Deleting the direct
    per-space marker, the mutable activation fence, and the source-side member
    incarnation must therefore not reclassify the source as legacy and permit
    an e+3 membership mutation.
    """

    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    (
        a_service,
        b_service,
        _a_config,
        b_config,
        a_storage,
        _b_storage,
        peers,
        pair_id,
    ) = await _drive_to_approved(235, 236)

    class _DelayActivation(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            if path.endswith("/events"):
                # Keep the signed e+2 on the wire for a later target retry,
                # while making the source durably enter blocked recovery.
                return PeerResponse(503, [], b"")
            return await super().send(method, path, headers=headers, body=body)

    a_service._sender_factory = lambda _endpoint: _DelayActivation(  # type: ignore[method-assign]
        peers, "B"
    )
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != MeshPairingState.ACTIVE.value
    source_session = await a_service.store.get_session(pair_id)
    assert source_session is not None
    assert source_session.state == MeshPairingState.BLOCKED_RECOVERY.value
    assert await a_service.store.get_source_bootstrap_evidence(pair_id) is not None

    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await source_store.get_membership()
    assert membership is not None and membership.epoch == source_session.base_epoch + 2
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    source_node_id = next(
        member.node_id for member in membership.members if member.node_id != target_node_id
    )
    await a_storage.delete(a_service.store._source_activation_marker_key(SPACE))
    await a_storage.delete(a_service.store._activation_fence_key(SPACE))
    await source_store.set_membership(
        membership.model_copy(
            update={
                "members": [
                    member.model_copy(update={"incarnation": None})
                    if member.node_id == target_node_id
                    else member
                    for member in membership.members
                ]
            }
        )
    )

    restarted = MeshPairingService(
        a_service._config,
        a_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=a_service._sender_factory,
    )
    register_pairing_activation_checker(restarted.assert_no_pairing_activation)
    try:
        with pytest.raises(PairingActivationError):
            await restarted._membership(SPACE).update_member_scopes(
                source_node_id, ["read"]
            )
    finally:
        clear_pairing_activation_checker()
    held = await source_store.get_membership()
    assert held is not None and held.epoch == membership.epoch


async def test_target_acceptance_intent_fences_lost_terminal_triplet() -> None:
    """A current target tail cannot be reclassified as receipt-less legacy data."""

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    (
        a_service,
        b_service,
        _a_config,
        b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(228, 229)
    real_put_terminal = b_service.store.put_target_terminal_confirmation
    fail = {"on": True}

    async def fail_terminal_confirmation(signed):
        if fail["on"]:
            fail["on"] = False
            raise OSError("simulated target terminal-confirmation crash")
        await real_put_terminal(signed)

    b_service.store.put_target_terminal_confirmation = fail_terminal_confirmation  # type: ignore[method-assign]
    result = await b_service.run_target_enrollment(pair_id)
    b_service.store.put_target_terminal_confirmation = real_put_terminal  # type: ignore[method-assign]
    assert result["ack_status"] != 200
    assert (await a_service.store.get_session(pair_id)).state == "active"

    # Simulate independent object loss and a process restart.  The immutable
    # acceptance intent remains the #417 provenance discriminator.
    await b_storage.delete(b_service.store._reservation_key(SPACE))
    await b_storage.delete(b_service.store._target_activation_receipt_key(pair_id))
    await b_storage.delete(b_service.store._source_activation_receipt_key(pair_id))
    await b_storage.delete(
        b_service.store._target_terminal_confirmation_key(pair_id)
    )
    restarted = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=b_service._sender_factory,
    )

    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.assert_space_not_reserved(SPACE)
    assert exc.value.code == "space_reserved"


@pytest.mark.parametrize(
    "lost_direct_record", ["none", "fence", "current_tail", "floor"]
)
async def test_target_fence_fails_closed_when_raw_reservation_is_lost_before_terminal(
    lost_direct_record: str,
) -> None:
    """A held #417 target tail never falls back to a raw-reservation miss.

    The direct target fence is written before the raw reservation.  Losing the
    raw reservation alone must therefore remain write-blocking; losing either
    half of the signed per-space index at the same time must also fail closed
    while its sibling still proves this is a current #417 target.
    """

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    (
        _a_service,
        b_service,
        _a_config,
        b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(240, 241)

    held_fence = await b_service.store.get_target_pairing_fence(SPACE)
    floor = await b_service.store.get_target_pairing_protocol_floor(SPACE)
    assert held_fence is not None and held_fence.authority.pair_id == pair_id
    assert held_fence.authority.phase == "held"
    assert floor is not None
    assert await b_service.store.get_reservation(SPACE) == pair_id

    # Simulate a restart after independent object loss, before target terminal
    # confirmation.  `assert_space_not_reserved` reads the raw reservation
    # first, so the direct fence/floor must independently preserve the block.
    await b_storage.delete(b_service.store._reservation_key(SPACE))
    if lost_direct_record == "fence":
        await b_storage.delete(b_service.store._target_pairing_fence_key(SPACE))
    elif lost_direct_record == "current_tail":
        await b_storage.delete(
            b_service.store._target_pairing_current_tail_key(SPACE)
        )
    elif lost_direct_record == "floor":
        await b_storage.delete(
            b_service.store._target_pairing_protocol_floor_key(SPACE)
        )

    restarted = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=lambda: NOW_MS + 1,
        sender_factory=b_service._sender_factory,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.assert_space_not_reserved(SPACE)
    assert exc.value.code == "space_reserved"


@pytest.mark.parametrize("replayed_record", ["fence", "current_tail"])
async def test_target_current_tail_rejects_historical_released_record_replay(
    replayed_record: str,
) -> None:
    """A prior released target pair cannot mask a later held tail.

    The permanent protocol floor tells the guard this instance has entered
    #417, while the current-tail index names the latest pairing generation.
    Replaying only one old signed direct record must therefore remain fenced
    after the raw operational reservation is independently lost.
    """

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    (
        a_service,
        b_service,
        _a_config,
        b_config,
        _a_storage,
        b_storage,
        _peers,
        first_pair,
    ) = await _drive_to_approved(244, 245)
    # Build P0's released tail through the real post-T1 recovery path.  Once
    # source membership has admitted a PENDING target, an ordinary cancel is
    # intentionally refused; source eviction emits the signed disposition that
    # authorizes target abandonment.
    b_service._sender_factory = lambda _e: _AckDropSender(_peers, "A")  # type: ignore[attr-defined]
    await b_service.run_target_enrollment(first_pair)
    await a_service.evict(first_pair, operator="op", reason="release P0")
    await b_service.abandon(first_pair)
    historical_fence = await b_service.store.get_target_pairing_fence(SPACE)
    historical_current = await b_service.store.get_target_pairing_current_tail(SPACE)
    assert historical_fence is not None and historical_fence.authority.phase == "released"
    assert historical_current == historical_fence

    second = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    second_pair = second["pair_id"]
    await b_service.accept_invitation(
        second["invitation_bytes"],
        SPACE,
        secret=second["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )
    current = await b_service.store.get_target_pairing_current_tail(SPACE)
    held = await b_service.store.get_target_pairing_fence(SPACE)
    assert current is not None and held is not None
    assert current == held and held.authority.pair_id == second_pair
    assert held.authority.phase == "held"
    await b_storage.delete(b_service.store._reservation_key(SPACE))

    if replayed_record == "fence":
        await b_storage.put(
            b_service.store._target_pairing_fence_key(SPACE),
            historical_fence.canonical_bytes().decode("utf-8"),
        )
    else:
        await b_storage.put(
            b_service.store._target_pairing_current_tail_key(SPACE),
            historical_current.canonical_bytes().decode("utf-8"),
        )

    restarted = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=lambda: NOW_MS + 1,
        sender_factory=b_service._sender_factory,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.assert_space_not_reserved(SPACE)
    assert exc.value.code == "space_reserved"


@pytest.mark.parametrize("replayed_record", ["fence", "current_tail"])
async def test_target_current_tail_rejects_same_pair_terminal_replay_after_rearm(
    replayed_record: str,
) -> None:
    """A terminal proof for the same pair cannot undo a recovery-held fence."""

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    (
        _a_service,
        b_service,
        _a_config,
        b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(246, 247)
    assert (await b_service.run_target_enrollment(pair_id))["state"] == "active"
    terminal_fence = await b_service.store.get_target_pairing_fence(SPACE)
    terminal_current = await b_service.store.get_target_pairing_current_tail(SPACE)
    assert terminal_fence is not None and terminal_fence.authority.phase == "terminal_confirmed"
    assert terminal_current == terminal_fence
    session = await b_service.store.get_session(pair_id)
    assert session is not None
    for key in (
        b_service.store._target_activation_receipt_key(pair_id),
        b_service.store._source_activation_receipt_key(pair_id),
        b_service.store._target_terminal_confirmation_key(pair_id),
    ):
        await b_storage.delete(key)
    assert await b_service._fence_active_target_terminal_chain_loss(
        session, base=session.base_epoch
    )
    held = await b_service.store.get_target_pairing_fence(SPACE)
    current = await b_service.store.get_target_pairing_current_tail(SPACE)
    assert held is not None and held.authority.phase == "held"
    assert current == held
    await b_storage.delete(b_service.store._reservation_key(SPACE))
    if replayed_record == "fence":
        await b_storage.put(
            b_service.store._target_pairing_fence_key(SPACE),
            terminal_fence.canonical_bytes().decode("utf-8"),
        )
    else:
        await b_storage.put(
            b_service.store._target_pairing_current_tail_key(SPACE),
            terminal_current.canonical_bytes().decode("utf-8"),
        )
    restarted = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=lambda: NOW_MS + 1,
        sender_factory=b_service._sender_factory,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.assert_space_not_reserved(SPACE)
    assert exc.value.code == "space_reserved"


@pytest.mark.parametrize("loss_path", ["terminal_chain", "activation_authority"])
async def test_target_recovery_rearm_keeps_direct_fence_after_raw_reservation_loss(
    loss_path: str,
) -> None:
    """Recovery must re-arm the signed fence before reserving the raw target.

    A raw reservation is a cache-like operational guard, not durable
    ordinary-write authority.  Both terminal-chain and activation-authority
    recovery paths therefore have to replace a prior terminal fence with an
    exact same-pair ``held`` fence.  Deleting the raw record after that recovery
    must still refuse an ordinary write after restart.
    """

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    (
        _a_service,
        b_service,
        _a_config,
        b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(246, 247)
    assert (await b_service.run_target_enrollment(pair_id))["state"] == (
        MeshPairingState.ACTIVE.value
    )
    session = await b_service.store.get_session(pair_id)
    assert session is not None and session.state == MeshPairingState.ACTIVE.value
    assert await b_service.store.get_reservation(SPACE) is None

    # Lose every terminal receipt.  The authority-loss branch additionally
    # loses the retained e+1 validation marker, whereas a post-all-ACK terminal
    # chain loss retains it but may no longer use it as completion authority.
    for key in (
        b_service.store._target_activation_receipt_key(pair_id),
        b_service.store._source_activation_receipt_key(pair_id),
        b_service.store._target_terminal_confirmation_key(pair_id),
    ):
        await b_storage.delete(key)
    if loss_path == "terminal_chain":
        rearmed = await b_service._fence_active_target_terminal_chain_loss(
            session, base=session.base_epoch
        )
    else:
        await b_storage.delete(b_service.store._import_validation_key(pair_id))
        rearmed = await b_service._fence_target_activation_authority_loss(
            session, base=session.base_epoch
        )

    assert rearmed is True
    held = await b_service.store.get_target_pairing_fence(SPACE)
    assert held is not None
    assert held.authority.pair_id == pair_id
    assert held.authority.phase == "held"
    assert await b_service.store.get_reservation(SPACE) == pair_id

    # Re-open the service after independent raw-record loss so no in-process
    # reservation cache can hide a missing direct target fence.
    await b_storage.delete(b_service.store._reservation_key(SPACE))
    restarted = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=lambda: NOW_MS + 1,
        sender_factory=b_service._sender_factory,
    )
    with pytest.raises(MeshPairingStoreError) as exc:
        await restarted.assert_space_not_reserved(SPACE)
    assert exc.value.code == "space_reserved"


@pytest.mark.parametrize("loss_path", ["terminal_chain", "activation_authority"])
async def test_target_recovery_persists_held_fence_before_unsafe_status_or_raw_reserve(
    loss_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovery crash point after arming already refuses ordinary writes.

    ``NodeHealth(UNSAFE)`` is operational state and the raw reservation can be
    lost independently.  Pausing that health write models a crash exactly
    between durable recovery steps: at that point the signed direct fence must
    already be ``held``, while the raw reservation has not yet been recreated.
    """

    from live_mem.mesh.pairing_store import MeshPairingStoreError

    (
        _a_service,
        b_service,
        _a_config,
        _b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(248, 249)
    assert (await b_service.run_target_enrollment(pair_id))["state"] == (
        MeshPairingState.ACTIVE.value
    )
    session = await b_service.store.get_session(pair_id)
    assert session is not None and session.state == MeshPairingState.ACTIVE.value

    for key in (
        b_service.store._target_activation_receipt_key(pair_id),
        b_service.store._source_activation_receipt_key(pair_id),
        b_service.store._target_terminal_confirmation_key(pair_id),
    ):
        await b_storage.delete(key)
    if loss_path == "activation_authority":
        await b_storage.delete(b_service.store._import_validation_key(pair_id))

    real_set_node_status = HivemindStateStore.set_node_status
    observed_held_before_unsafe = False

    async def crash_after_fence_before_unsafe(store, health):
        nonlocal observed_held_before_unsafe
        if store.space_id == SPACE and health.status == HiveNodeStatus.UNSAFE:
            with pytest.raises(MeshPairingStoreError) as exc:
                await b_service.assert_space_not_reserved(SPACE)
            assert exc.value.code == "space_reserved"
            observed_held_before_unsafe = True
            raise OSError("simulated crash after target fence re-arm")
        return await real_set_node_status(store, health)

    monkeypatch.setattr(
        HivemindStateStore, "set_node_status", crash_after_fence_before_unsafe
    )
    if loss_path == "terminal_chain":
        rearmed = await b_service._fence_active_target_terminal_chain_loss(
            session, base=session.base_epoch
        )
    else:
        rearmed = await b_service._fence_target_activation_authority_loss(
            session, base=session.base_epoch
        )

    # The injected I/O failure is deliberately caught by the recovery helper.
    # Its durable precondition, not a later health/raw-reservation write, is
    # what keeps the target ordinary-write fenced across that crash.
    assert rearmed is False
    assert observed_held_before_unsafe is True
    held = await b_service.store.get_target_pairing_fence(SPACE)
    assert held is not None
    assert held.authority.pair_id == pair_id
    assert held.authority.phase == "held"
    assert await b_service.store.get_reservation(SPACE) is None
    health = await HivemindStateStore(
        storage=b_storage, space_id=SPACE
    ).get_node_status()  # type: ignore[arg-type]
    assert health is not None and health.status == HiveNodeStatus.HEALTHY.value
    with pytest.raises(MeshPairingStoreError) as exc:
        await b_service.assert_space_not_reserved(SPACE)
    assert exc.value.code == "space_reserved"


async def test_target_fence_guard_avoids_session_inventory_above_history_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed and unrelated target writes remain O(1) above 256 history rows."""

    (
        _a_service,
        b_service,
        _a_config,
        _b_config,
        _a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(242, 243)
    completed = await b_service.run_target_enrollment(pair_id)
    assert completed["state"] == MeshPairingState.ACTIVE.value
    target_session = await b_service.store.get_session(pair_id)
    terminal_fence = await b_service.store.get_target_pairing_fence(SPACE)
    assert target_session is not None
    assert terminal_fence is not None
    assert terminal_fence.authority.phase == "terminal_confirmed"
    assert await b_service.store.get_reservation(SPACE) is None

    # Make the former global inventory path unrepresentable: 257 historic
    # target records exceed MAX_PAIRING_SESSIONS before the current terminal
    # record is considered.  They must be irrelevant to either direct guard.
    for index in range(1, 258):
        historical_id = f"pair_{index:032x}"
        if historical_id == pair_id:
            continue
        await b_service.store.put_session(
            replace(
                target_session,
                pair_id=historical_id,
                state=MeshPairingState.CANCELLED.value,
                updated_at_ms=NOW_MS + index,
            )
        )

    async def history_scan_forbidden(*_args, **_kwargs):
        raise AssertionError("ordinary target writes must not call list_sessions")

    real_list_objects = b_storage.list_objects

    async def session_inventory_forbidden(prefix: str, max_keys: int = 0):
        if prefix == f"{b_service.store._prefix}sessions/":
            raise AssertionError("ordinary target writes must not list session objects")
        return await real_list_objects(prefix, max_keys)

    monkeypatch.setattr(b_service.store, "list_sessions", history_scan_forbidden)
    monkeypatch.setattr(b_storage, "list_objects", session_inventory_forbidden)

    # A completed #417 target verifies only its signed per-space terminal
    # authority.  A legacy/unrelated space has neither record and likewise
    # must not inherit a global target-session ceiling.
    await b_service.assert_space_not_reserved(SPACE)
    await b_service.assert_space_not_reserved("unrelated")


async def test_completed_target_fence_guard_does_not_reparse_bootstrap_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal target guard consumes its signed fence, not retained bootstrap."""

    (
        _a_service,
        b_service,
        _a_config,
        _b_config,
        _a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(244, 245)
    completed = await b_service.run_target_enrollment(pair_id)
    assert completed["state"] == MeshPairingState.ACTIVE.value
    terminal_fence = await b_service.store.get_target_pairing_fence(SPACE)
    assert terminal_fence is not None
    assert terminal_fence.authority.pair_id == pair_id
    assert terminal_fence.authority.phase == "terminal_confirmed"

    async def retained_bootstrap_reparse_forbidden(*_args, **_kwargs):
        raise AssertionError(
            "ordinary completed-target guard must not reparse retained bootstrap"
        )

    # The historical guard reached `_retained_import_authority` through
    # `_target_finalized_activation_matches`, which parses and hashes the
    # retained bootstrap payload.  The direct signed fence makes that expensive
    # replay path unnecessary for each ordinary write.
    monkeypatch.setattr(
        b_service,
        "_retained_import_authority",
        retained_bootstrap_reparse_forbidden,
    )
    await b_service.assert_space_not_reserved(SPACE)


async def test_terminal_replay_restores_lost_target_triplet_after_normal_commit() -> None:
    """A completed target repairs its exact terminal chain after later work.

    The e+1 pointer is intentionally allowed to advance after all-ACK.  If the
    target then loses its reservation and terminal copies, it must re-fence and
    accept only the source's replay carrying the original source receipt and
    this target's old detached terminal confirmation — not infer success from a
    mutable ACTIVE session or the newer BANK_COMMIT head.
    """

    (
        a_service,
        b_service,
        _a_config,
        b_config,
        a_storage,
        b_storage,
        peers,
        pair_id,
    ) = await _drive_to_approved(233, 234)
    assert (await b_service.run_target_enrollment(pair_id))["ack_status"] == 200

    target_before = await b_service.store.get_target_activation_receipt(pair_id)
    source_before = await b_service.store.get_source_activation_receipt(pair_id)
    terminal_before = await b_service.store.get_target_terminal_confirmation(pair_id)
    assert target_before is not None
    assert source_before is not None
    assert terminal_before is not None

    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    source_node = await source_store.get_node_identity()
    assert source_node is not None
    for store in (source_store, target_store):
        membership = await store.get_membership()
        term = await store.get_term()
        assert membership is not None and term is not None
        commit = BankCommit(
            bank_version=2,
            parent_bank_version=1,
            term=term.term,
            membership_epoch=membership.epoch,
            commit_id="post-terminal-c2",
            committed_by_node_id=source_node.node_id,
            manifest=[],
        )
        await store.append_commit(commit)
        await store.set_bank_version_pointer(
            BankVersionPointer(bank_version=2, commit_id=commit.commit_id)
        )

    # Simulate a restart after independent loss of every mutable target-tail
    # object.  The #417 import marker and immutable acceptance intent remain;
    # they fence ordinary writes while the source proves the completed chain.
    await b_storage.delete(b_service.store._reservation_key(SPACE))
    await b_storage.delete(b_service.store._target_activation_receipt_key(pair_id))
    await b_storage.delete(b_service.store._source_activation_receipt_key(pair_id))
    await b_storage.delete(b_service.store._target_terminal_confirmation_key(pair_id))
    restarted = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=b_service._sender_factory,
    )
    peers["B"] = MeshNamespaceRouter(
        _fallback(),
        config=b_config,
        process_lock=FakeProcessLock(),
        storage_factory=lambda: b_storage,
        replay_ledger=FakeReplayLedger(),
        clock_ms=lambda: NOW_MS,
        pairing_service=restarted,
    )

    repaired = await restarted.run_target_enrollment(pair_id)

    assert repaired["ack_status"] == 200
    health = await target_store.get_node_status()
    assert health is not None and health.status == HiveNodeStatus.HEALTHY.value
    assert await restarted.store.get_reservation(SPACE) is None
    target_after = await restarted.store.get_target_activation_receipt(pair_id)
    source_after = await restarted.store.get_source_activation_receipt(pair_id)
    terminal_after = await restarted.store.get_target_terminal_confirmation(pair_id)
    assert target_after is not None and target_after.canonical_bytes() == target_before.canonical_bytes()
    assert source_after is not None and source_after.canonical_bytes() == source_before.canonical_bytes()
    assert terminal_after is not None and terminal_after.canonical_bytes() == terminal_before.canonical_bytes()


async def test_bare_reconfirmation_hydrates_target_receipt_before_terminal_replay() -> None:
    """Crossed source/target receipt loss cannot make retry order a deadlock.

    Once all-ACK is complete, a normal v2 BANK_COMMIT may advance the head.  If
    A loses its source receipt while B loses only its detached target receipt,
    A's first retry is deliberately a bare e+2 request.  B must rehydrate its
    target receipt from its already-retained source+terminal chain *before*
    fencing that bare retry, so A can consume the returned exact terminal chain.
    """

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(234, 235)
    assert (await b_service.run_target_enrollment(pair_id))["ack_status"] == 200

    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    source_node = await source_store.get_node_identity()
    assert source_node is not None
    for store in (source_store, target_store):
        membership = await store.get_membership()
        term = await store.get_term()
        assert membership is not None and term is not None
        commit = BankCommit(
            bank_version=2,
            parent_bank_version=1,
            term=term.term,
            membership_epoch=membership.epoch,
            commit_id="cross-loss-post-terminal-c2",
            committed_by_node_id=source_node.node_id,
            manifest=[],
        )
        await store.append_commit(commit)
        await store.set_bank_version_pointer(
            BankVersionPointer(bank_version=2, commit_id=commit.commit_id)
        )

    # Keep B's source receipt and target-signed terminal confirmation, but
    # remove exactly the detached artifacts that force the bare-first retry.
    await a_storage.delete(a_service.store._source_activation_receipt_key(pair_id))
    await b_storage.delete(b_service.store._target_activation_receipt_key(pair_id))
    assert await b_service.store.get_source_activation_receipt(pair_id) is not None
    assert await b_service.store.get_target_terminal_confirmation(pair_id) is not None

    repaired = await a_service.resume(pair_id)

    assert repaired["state"] == MeshPairingState.ACTIVE.value
    assert repaired.get("source_confirmation_pending") is not True
    assert await a_service.store.get_activation_fence(SPACE) is None
    assert await b_service.store.get_reservation(SPACE) is None
    assert await b_service.store.get_target_activation_receipt(pair_id) is not None
    assert await a_service.store.get_source_activation_receipt(pair_id) is not None
    assert await a_service.store.get_target_terminal_confirmation(pair_id) is not None
    health = await target_store.get_node_status()
    assert health is not None and health.status == HiveNodeStatus.HEALTHY.value


async def test_source_reconfirmation_restores_full_terminal_chain_after_normal_commit() -> None:
    """A source that lost both terminal copies restores only B's exact chain."""

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(236, 237)
    assert (await b_service.run_target_enrollment(pair_id))["ack_status"] == 200

    source_before = await a_service.store.get_source_activation_receipt(pair_id)
    terminal_before = await a_service.store.get_target_terminal_confirmation(pair_id)
    assert source_before is not None and terminal_before is not None
    _source_store, target_store = await _advance_completed_pair_head(
        a_storage, b_storage, commit_id="source-terminal-loss-c2"
    )

    # A normal BANK_COMMIT must not make the target's retained signed terminal
    # chain unusable.  The source has no local completion copy to replay, so its
    # first request is deliberately bare and can only accept B's exact response.
    await a_storage.delete(a_service.store._source_activation_receipt_key(pair_id))
    await a_storage.delete(
        a_service.store._target_terminal_confirmation_key(pair_id)
    )

    repaired = await a_service.resume(pair_id)

    assert repaired["state"] == MeshPairingState.ACTIVE.value
    assert repaired.get("source_confirmation_pending") is not True
    source_after = await a_service.store.get_source_activation_receipt(pair_id)
    terminal_after = await a_service.store.get_target_terminal_confirmation(pair_id)
    assert source_after is not None and terminal_after is not None
    assert source_after.canonical_bytes() == source_before.canonical_bytes()
    assert terminal_after.canonical_bytes() == terminal_before.canonical_bytes()
    assert await a_service.store.get_activation_fence(SPACE) is None
    pointer = await target_store.get_bank_version_pointer()
    assert pointer is not None and pointer.commit_id == "source-terminal-loss-c2"


async def test_source_reconfirmation_rejects_conflicting_target_terminal_chain_after_normal_commit() -> None:
    """A re-signed but incompatible target terminal record cannot heal A's loss."""

    from live_mem.core.reservation_guard import PairingActivationError

    (
        a_service,
        b_service,
        _a_config,
        b_config,
        a_storage,
        b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(237, 238)
    assert (await b_service.run_target_enrollment(pair_id))["ack_status"] == 200

    terminal = await b_service.store.get_target_terminal_confirmation(pair_id)
    assert terminal is not None
    _source_store, target_store = await _advance_completed_pair_head(
        a_storage, b_storage, commit_id="conflicting-terminal-c2"
    )

    # This remains a well-formed target-signed record, but it binds a different
    # source receipt digest.  Raw object replacement models a conflicting
    # persisted record without relying on malformed JSON to exercise rejection.
    conflicting = SignedTargetTerminalConfirmationReceipt.sign(
        replace(
            terminal.receipt,
            source_activation_receipt_digest="0" * 64,
        ),
        b_config.private_key,
    )
    conflicting_bytes = conflicting.canonical_bytes().decode("utf-8")
    await b_storage.put(
        b_service.store._target_terminal_confirmation_key(pair_id),
        conflicting_bytes,
    )
    await a_storage.delete(a_service.store._source_activation_receipt_key(pair_id))
    await a_storage.delete(
        a_service.store._target_terminal_confirmation_key(pair_id)
    )

    repaired = await a_service.resume(pair_id)

    assert repaired["state"] == MeshPairingState.ACTIVE.value
    assert repaired["source_confirmation_pending"] is True
    assert await a_service.store.get_target_terminal_confirmation(pair_id) is None
    assert await a_service.store.get_activation_fence(SPACE) == pair_id
    assert (
        await b_storage.get(b_service.store._target_terminal_confirmation_key(pair_id))
        == conflicting_bytes
    )
    with pytest.raises(PairingActivationError):
        await a_service.assert_no_pairing_activation(SPACE)
    pointer = await target_store.get_bank_version_pointer()
    assert pointer is not None and pointer.commit_id == "conflicting-terminal-c2"


async def test_active_target_retry_repairs_local_tail_while_source_is_offline() -> None:
    """A restart repairs its local tail but reports source confirmation pending."""

    a_service, b_service, _a_config, b_config, _a_storage, b_storage, _peers, pair_id = (
        await _drive_to_approved(87, 88)
    )
    real_release = b_service.store.release
    fail_release = {"on": True}

    async def fail_first_target_release(space_id, released_pair_id):
        if (
            fail_release["on"]
            and space_id == SPACE
            and released_pair_id == pair_id
        ):
            fail_release["on"] = False
            raise OSError("simulated target reservation release failure")
        await real_release(space_id, released_pair_id)

    b_service.store.release = fail_first_target_release  # type: ignore[method-assign]
    initial = await b_service.run_target_enrollment(pair_id)
    b_service.store.release = real_release  # type: ignore[method-assign]
    assert initial["state"] == "active"
    assert await b_service.store.get_reservation(SPACE) == pair_id
    assert (await a_service.store.get_session(pair_id)).state == "active"

    class _Offline:
        async def send(self, method, path, *, headers, body):
            raise AssertionError("an ACTIVE local-tail retry must not call the source")

    # Re-open the service against the same durable storage to prove this is a
    # restart-safe local convergence tail, not an in-process retry artifact.
    restarted = MeshPairingService(
        b_config,
        b_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=lambda _endpoint: _Offline(),
    )
    repaired = await restarted.run_target_enrollment(pair_id)

    assert repaired == {
        "pair_id": pair_id,
        "state": "active",
        "ack_status": 202,
        "source_confirmation_pending": True,
    }
    # The target cannot locally release its ordinary-write fence while the
    # source is offline: only the source-signed terminal receipt can do that.
    assert await restarted.store.get_reservation(SPACE) == pair_id
    assert (await a_service.store.get_session(pair_id)).state == "active"


async def test_resync_refuses_tampered_active_session_without_local_e2_authority() -> None:
    """An ACTIVE workflow record alone cannot bless an e+1 target as healthy."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, _peers, pair_id = (
        await _drive_to_approved(86, 87)
    )
    claimed = await b_service.store.get_session(pair_id)
    assert claimed is not None and claimed.state == "claimed"
    signed_env = await b_service._fetch_and_verify_approval(claimed)
    transferring = claimed.transition(
        MeshPairingState.APPROVED, now_ms=NOW_MS
    ).transition(
        MeshPairingState.TRANSFERRING,
        now_ms=NOW_MS,
        bootstrap_manifest_digest=signed_env.envelope.manifest_digest,
        bootstrap_bank_version=signed_env.envelope.bank_version,
    )
    await b_service.store.put_session(transferring)
    awaiting = await b_service._import_and_await(transferring, signed_env)
    # Simulate a valid-schema persistence rewrite: the imported target is only
    # e+1/PENDING, but workflow state falsely claims a completed e+2 receipt.
    await b_service.store.put_session(
        awaiting.transition(MeshPairingState.ACTIVE, now_ms=NOW_MS)
    )
    before = b_storage.snapshot()

    with pytest.raises(MeshPairingServiceError) as exc:
        await b_service.resync(pair_id)

    assert exc.value.code == "not_resyncable"
    assert b_storage.snapshot() == before
    target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    health = await target_store.get_node_status()
    membership = await target_store.get_membership()
    assert health is not None and HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE
    assert membership is not None and membership.epoch == 2
    assert await b_service.store.get_reservation(SPACE) == pair_id
    assert (await a_service.store.get_session(pair_id)).state == "transferring"


async def test_active_source_ack_retry_redelivers_before_success() -> None:
    """A rewritten source ACTIVE record cannot suppress the target all-ACK proof."""

    a_service, b_service, _a_config, _b_config, _a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(191, 192)
    )

    class _BlackHole:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(503, [], b"")

    a_service._sender_factory = lambda _endpoint: _BlackHole()  # type: ignore[attr-defined]
    initial = await b_service.run_target_enrollment(pair_id)
    assert initial["ack_status"] != 200
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"
    assert (await b_service.store.get_session(pair_id)).state == "awaiting_acks"

    a_service._sender_factory = lambda _endpoint: AsgiPeerSender(peers, "B")  # type: ignore[attr-defined]
    source = await a_service.store.get_session(pair_id)
    assert source is not None
    # Valid-schema storage damage changes only operational workflow state; the
    # source membership is already e+2 while the target remains e+1/PENDING.
    await a_service.store.put_session(
        source.with_fields(now_ms=source.updated_at_ms + 1, state="active")
    )

    retried = await b_service.run_target_enrollment(pair_id)

    assert retried["ack_status"] == 200
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    health = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert health is not None and HiveNodeStatus(health.status) == HiveNodeStatus.HEALTHY
    assert await b_service.store.get_reservation(SPACE) is None


async def test_missing_import_marker_blocks_replayed_active_receipt_tail() -> None:
    """A rewritten target ACTIVE receipt cannot bypass retained import authority."""

    a_service, b_service, _a_config, _b_config, a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(193, 194)
    )

    class _BlackHole:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(503, [], b"")

    a_service._sender_factory = lambda _endpoint: _BlackHole()  # type: ignore[method-assign]
    initial = await b_service.run_target_enrollment(pair_id)
    assert initial["state"] == MeshPairingState.AWAITING_ACKS.value
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"
    target = await b_service.store.get_session(pair_id)
    assert target is not None and target.state == MeshPairingState.AWAITING_ACKS.value

    # Simulate coherent-looking but unauthoritative storage damage: target
    # membership matches the source e+2 view and its workflow record claims
    # ACTIVE, but the independently retained import marker is gone.
    source_membership = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_membership()  # type: ignore[arg-type]
    assert source_membership is not None and source_membership.epoch == target.base_epoch + 2
    await b_service.store.clear_import_validation_for_resync(pair_id)
    await b_service.store.put_session(
        target.transition(MeshPairingState.ACTIVE, now_ms=NOW_MS)
    )
    target_store = HivemindStateStore(storage=b_storage, space_id=SPACE)  # type: ignore[arg-type]
    await target_store.set_membership(source_membership)

    a_service._sender_factory = lambda _endpoint: AsgiPeerSender(peers, "B")  # type: ignore[method-assign]
    resumed = await a_service.resume(pair_id)

    assert resumed["state"] != MeshPairingState.ACTIVE.value
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"
    assert (await b_service.store.get_session(pair_id)).state == MeshPairingState.ACTIVE.value
    health = await target_store.get_node_status()
    assert health is not None and HiveNodeStatus(health.status) == HiveNodeStatus.UNSAFE
    assert await b_service.store.get_reservation(SPACE) == pair_id
    assert await b_service.store.get_import_validation(pair_id) is None


async def test_corrupt_import_resync_reaches_active_without_eviction(monkeypatch):
    # A corrupt bootstrap blocks the target (resync evidence). A subsequent resync
    # tears the target back to blank, re-imports a CLEAN snapshot from the still-
    # transferring source, and re-drives to ACTIVE — the promised recovery action,
    # with NO source-side eviction of the admitted pending member.
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(85, 86)
    fail = {"on": True}

    class _CorruptOnce(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            resp = await super().send(method, path, headers=headers, body=body)
            if fail["on"] and path.endswith("/bootstrap"):
                return PeerResponse(resp.status_code, resp.headers, resp.body + b"x")
            return resp

    b_service._sender_factory = lambda _e: _CorruptOnce(peers, "A")  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        await b_service.run_target_enrollment(pair_id)
    assert (await b_service.store.get_session(pair_id)).state == "blocked_recovery"

    fail["on"] = False  # source now serves a clean bootstrap
    out = await b_service.resync(pair_id)
    assert out["state"] == "active", out
    assert (await a_service.store.get_session(pair_id)).state == "active"
    assert (await b_service.store.get_session(pair_id)).state == "active"
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert any(m.node_id == tgt and m.status == "active" for m in a_mem.members)  # not evicted
    assert await b_service.store.get_reservation(SPACE) is None


async def test_hardcrash_mid_import_transferring_resync_reaches_active(monkeypatch):
    # A HARD crash (BaseException) mid-import leaves the target durably in
    # 'transferring' with NO signed evidence and a non-blank space — a state the
    # except-block never sees. resync must still recover it: synthesize evidence
    # (transferring -> blocked_recovery), tear down the partial import, re-import,
    # and re-drive to active, without any source-side eviction.
    import live_mem.mesh.pairing_service as ps_mod

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(87, 88)
    real_import = ps_mod.import_bootstrap
    calls = {"n": 0}

    class _HardCrash(BaseException):
        pass

    async def flaky_import(bootstrap_service, space_id, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            await b_storage.put(f"{space_id}/_hivemind/node.json", '{"node_id":"n","public_key":"ed25519:AAA"}')
            await b_storage.put(f"{space_id}/bank/partial.md", "partial")
            raise _HardCrash("killed mid-import")
        return await real_import(bootstrap_service, space_id, **kw)

    monkeypatch.setattr(ps_mod, "import_bootstrap", flaky_import)
    with pytest.raises(_HardCrash):
        await b_service.run_target_enrollment(pair_id)
    stuck = await b_service.store.get_session(pair_id)
    assert stuck.state == "transferring"  # dead-end shape...
    assert await b_service.store.get_evidence(pair_id) is None  # ...with NO evidence

    out = await b_service.resync(pair_id)
    assert out["state"] == "active", out
    assert (await a_service.store.get_session(pair_id)).state == "active"
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert any(m.node_id == tgt and m.status == "active" for m in a_mem.members)  # not evicted


async def test_teardown_target_space_deletes_nonplaceholders_status_marker_last(monkeypatch):
    import json as _json

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(89, 90)
    st = b_service._storage_factory()
    await st.put(f"{SPACE}/_hivemind/node.json", _json.dumps({"node_id": "n", "public_key": "ed25519:AAA"}))
    await st.put(f"{SPACE}/_hivemind/members.json", _json.dumps({"epoch": 2, "members": []}))
    await st.put(f"{SPACE}/_hivemind/node_status.json", _json.dumps({"status": "unsafe"}))
    await st.put(f"{SPACE}/bank/imported.md", "data")

    order: list = []
    real_delete = st.delete

    async def rec_delete(key):
        order.append(key)
        await real_delete(key)

    st.delete = rec_delete  # type: ignore[method-assign]
    await b_service._teardown_target_space(SPACE)

    remaining = {o["Key"] for o in await st.list_objects(f"{SPACE}/")}
    assert remaining == {
        f"{SPACE}/_meta.json",
        f"{SPACE}/_rules.md",
        f"{SPACE}/live/.keep",
        f"{SPACE}/bank/.keep",
    }
    # The UNSAFE marker is deleted LAST so a mid-teardown crash never leaves the
    # space structurally-complete-but-unmarked (which would classify as HEALTHY).
    assert order[-1] == f"{SPACE}/_hivemind/node_status.json"


async def _admin_post(app, action, data, *, host=b"a.mesh.test"):
    import json as _json

    from live_mem.auth.context import current_token_info

    body = _json.dumps({"confirm": True, **data}).encode()
    headers = [(b"host", host), (b"origin", b"https://" + host)]
    scope = {"type": "http", "method": "POST", "path": "/api/admin/mesh/" + action, "headers": headers, "state": {}, "_body": body}
    messages: list = []

    async def receive():
        return {"type": "http.request", "body": scope.pop("_body", b""), "more_body": False}

    async def send(m):
        messages.append(m)

    tok = current_token_info.set({"permissions": ["admin", "read", "write"], "client_name": "op"})
    try:
        await app(scope, receive, send)
    finally:
        current_token_info.reset(tok)
    status = messages[0]["status"]
    payload = _json.loads(messages[1]["body"]) if len(messages) > 1 and messages[1].get("body") else {}
    return status, payload


async def test_admin_control_plane_completes_enrollment_two_instances(monkeypatch):
    # The full three actions driven ONLY through /api/admin/mesh/* on both
    # instances (never calling the service directly): create -> accept -> approve
    # -> ENROLL must converge, proving the control plane can complete Action 3.
    from live_mem.mesh.mesh_admin import MeshAdminMiddleware

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, _pair = await _drive_to_approved(95, 96)
    # _drive_to_approved already created+approved one pairing; run a fresh one end
    # to end purely via the admin plane on a second, clean pair of instances.
    a2, b2, a2_cfg, b2_cfg, a2_st, b2_st, peers2, _p = await _build_admin_instances(97, 98)
    a_admin = MeshAdminMiddleware(_fallback(), a2)
    b_admin = MeshAdminMiddleware(_fallback(), b2)

    status, inv = await _admin_post(a_admin, "invitation", {"space_id": SPACE, "scopes": ["read", "commit"]})
    assert status == 200 and inv["status"] == "ok"
    pair_id = inv["pair_id"]

    status, acc = await _admin_post(
        b_admin, "accept",
        {"invitation": inv["invitation"], "target_space_id": SPACE, "secret": inv["secret"], "source_endpoint": A_URL, "scopes": ["read", "commit"], "quiesced": True},
        host=b"b.mesh.test",
    )
    assert status == 200 and acc["state"] == "claimed"

    status, appr = await _admin_post(a_admin, "approve", {"pair_id": pair_id})
    assert status == 200 and appr["epoch"] == 2  # admit pending at e+1

    status, enr = await _admin_post(b_admin, "enroll", {"pair_id": pair_id}, host=b"b.mesh.test")
    assert status == 200, enr
    assert enr["state"] == "active", enr
    tgt = b2_cfg.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a2_st, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 3 and any(m.node_id == tgt and m.status == "active" for m in a_mem.members)


async def test_approve_export_failure_blocks_not_cancellable_and_evicts(monkeypatch):
    # A bootstrap export failure AFTER Transition 1 (admit) must NOT leave a
    # cancellable 'approved' session behind an already-admitted pending member;
    # it is blocked_recovery with signed evidence, cleanly given up by evict.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, _ = await _build_admin_instances(101, 102)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)

    async def boom(*a, **k):
        raise RuntimeError("simulated bootstrap export failure")

    monkeypatch.setattr(a_service, "_export_and_store_bootstrap", boom)
    with pytest.raises(MeshPairingServiceError):
        await a_service.approve(pair_id)

    sess = await a_service.store.get_session(pair_id)
    assert sess.state == "blocked_recovery"  # NOT a cancellable 'approved'
    ev = await a_service.store.get_evidence(pair_id)
    assert ev is not None and ev.evidence.phase == "bootstrap_export_failed"
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert any(m.node_id == tgt and m.status == "pending" for m in a_mem.members)  # admitted

    # An export-failed block is evict-only: resume must refuse (its target never
    # bootstrapped, so promoting it ACTIVE would be wrong), and force_evict_member
    # must refuse a PENDING candidate (use evict, not member-eviction).
    with pytest.raises(MeshPairingServiceError) as re:
        await a_service.resume(pair_id)
    assert re.value.code == "not_resumable"
    with pytest.raises(MeshPairingServiceError) as fe:
        await a_service.force_evict_member(pair_id, operator="op")
    assert fe.value.code == "not_active"

    out = await a_service.evict(pair_id, operator="op", reason="export failed")
    assert out["state"] == "cancelled"
    a_mem2 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    # Candidate removed from the ACTIVE∪PENDING roster (not stranded); an audit
    # record may remain with a non-roster status.
    assert not any(m.node_id == tgt and m.status in ("pending", "active") for m in a_mem2.members)


async def test_hard_crash_during_export_leaves_recoverable_approved_admitted(monkeypatch):
    # A HARD crash (uncatchable) DURING the bootstrap export — after Transition 1
    # but before the transferring persist — must leave the source durably
    # 'approved' with the target already admitted (NOT a durable dead-end
    # 'transferring'). cancel() must refuse (releasing would strand the pending
    # member); evict() recovers it.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, _ = await _build_admin_instances(103, 104)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)

    class _HardCrash(BaseException):
        pass

    async def hard_crash(*a, **k):
        # SIGKILL/power-loss mid-export: uncatchable, so the except handler that
        # would persist blocked_recovery never runs.
        raise _HardCrash("crash during bootstrap export, before the transferring persist")

    monkeypatch.setattr(a_service, "_export_and_store_bootstrap", hard_crash)
    with pytest.raises(_HardCrash):
        await a_service.approve(pair_id)

    sess = await a_service.store.get_session(pair_id)
    assert sess.state == "approved"  # durably 'approved' (NOT a dead-end transferring)...
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert any(m.node_id == tgt and m.status == "pending" for m in a_mem.members)  # ...but admitted

    with pytest.raises(MeshPairingServiceError) as e:
        await a_service.cancel(pair_id)
    assert e.value.code == "already_admitted"

    out = await a_service.evict(pair_id, operator="op")
    assert out["state"] == "cancelled"
    a_mem2 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert not any(m.node_id == tgt and m.status in ("pending", "active") for m in a_mem2.members)


async def test_evict_retries_approved_after_pending_removal_crash(monkeypatch):
    """A source crash after e+1 removal must not strand an APPROVED tail.

    The pre-removal intent is the only authority allowed to turn the adjacent
    EVICTED membership into the target-facing terminal disposition.  This
    specifically covers a hard crash during an export, where the source
    session has not reached ``blocked_recovery`` yet.
    """

    a_service, b_service, *_rest = await _build_admin_instances(181, 182)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read",), quiesced=True,
    )

    class _HardCrash(BaseException):
        pass

    async def crash_during_export(*_args, **_kwargs):
        raise _HardCrash("crash during bootstrap export")

    monkeypatch.setattr(a_service, "_export_and_store_bootstrap", crash_during_export)
    with pytest.raises(_HardCrash):
        await a_service.approve(pair_id)

    source = await a_service.store.get_session(pair_id)
    assert source is not None and source.state == MeshPairingState.APPROVED.value

    real_persist = a_service._persist_source_terminal_disposition

    async def crash_after_pending_removal(*_args, **_kwargs):
        raise _HardCrash("crash after pending removal")

    monkeypatch.setattr(
        a_service,
        "_persist_source_terminal_disposition",
        crash_after_pending_removal,
    )
    with pytest.raises(_HardCrash):
        await a_service.evict(pair_id, operator="op", reason="retry seam")

    assert await a_service.store.get_source_pending_eviction_intent(pair_id)
    assert await a_service.store.get_source_terminal_disposition(pair_id) is None

    monkeypatch.setattr(
        a_service, "_persist_source_terminal_disposition", real_persist
    )
    out = await a_service.evict(pair_id, operator="op", reason="retry")
    assert out["state"] == MeshPairingState.CANCELLED.value
    assert await a_service.store.get_source_terminal_disposition(pair_id)

    abandoned = await b_service.abandon(pair_id)
    assert abandoned["state"] == MeshPairingState.CANCELLED.value
    assert await b_service.store.get_reservation(SPACE) is None


async def test_hard_crash_after_source_marker_allows_only_owner_evict(monkeypatch):
    """The marker-before-TRANSFERRING crash prefix cannot strand e+1.

    The signed per-space marker is intentionally written before the bootstrap
    is advertised.  A power loss immediately after that write leaves the
    source session APPROVED while its target is already PENDING at e+1.  The
    marker must fence unrelated epoch changes, but the owning give-up path must
    still be able to remove exactly that candidate.
    """

    from live_mem.core.reservation_guard import PairingActivationError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, _ = (
        await _build_admin_instances(105, 106)
    )
    invite = await a_service.create_invitation(
        SPACE, requested_scopes=("read", "commit")
    )
    pair_id = invite["pair_id"]
    await b_service.accept_invitation(
        invite["invitation_bytes"],
        SPACE,
        secret=invite["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read", "commit"), quiesced=True,
    )

    class _HardCrash(BaseException):
        pass

    real_put_marker = a_service.store.put_source_activation_marker

    async def crash_after_marker(marker):
        await real_put_marker(marker)
        raise _HardCrash("crash after durable source activation marker")

    monkeypatch.setattr(
        a_service.store, "put_source_activation_marker", crash_after_marker
    )
    with pytest.raises(_HardCrash):
        await a_service.approve(pair_id)

    session = await a_service.store.get_session(pair_id)
    assert session is not None and session.state == MeshPairingState.APPROVED.value
    marker = await a_service.store.get_source_activation_marker(SPACE)
    assert marker is not None and marker.evidence.pair_id == pair_id
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    membership = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_membership()
    assert membership is not None and membership.epoch == session.base_epoch + 1
    assert any(
        member.node_id == target_node_id and member.status == "pending"
        for member in membership.members
    )

    # An ordinary mutation does not own the tail and remains fenced.
    with pytest.raises(PairingActivationError):
        await a_service.assert_no_pairing_activation(SPACE)

    # The paired operator eviction passes the exact pair id through the
    # lifecycle checker, removes only the PENDING candidate, and clears the
    # marker.  This is the sole permitted recovery for the crash prefix.
    out = await a_service.evict(pair_id, operator="op", reason="hard crash")
    assert out["state"] == MeshPairingState.CANCELLED.value
    assert await a_service.store.get_source_activation_marker(SPACE) is None
    membership = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_membership()
    assert membership is not None
    assert not any(
        member.node_id == target_node_id
        and member.status in ("pending", "active")
        for member in membership.members
    )


async def test_evict_retry_finishes_cancelled_marker_release_after_hard_crash(monkeypatch):
    """A crash after CANCELLED retains only a bounded, idempotent cleanup tail."""

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = (
        await _drive_to_approved(107, 108)
    )

    class _HardCrash(BaseException):
        pass

    real_release_marker = a_service.store.release_source_activation_marker

    async def crash_before_marker_release(space_id: str, owner_pair_id: str) -> None:
        assert space_id == SPACE and owner_pair_id == pair_id
        raise _HardCrash("crash after durable cancelled session")

    monkeypatch.setattr(
        a_service.store,
        "release_source_activation_marker",
        crash_before_marker_release,
    )
    with pytest.raises(_HardCrash):
        await a_service.evict(pair_id, operator="op", reason="simulate power loss")

    session = await a_service.store.get_session(pair_id)
    assert session is not None and session.state == MeshPairingState.CANCELLED.value
    marker = await a_service.store.get_source_activation_marker(SPACE)
    assert marker is not None and marker.evidence.pair_id == pair_id
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    membership = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_membership()
    assert membership is not None
    evicted = next(
        member for member in membership.members if member.node_id == target_node_id
    )
    assert evicted.status == "evicted" and evicted.incarnation == pair_id

    monkeypatch.setattr(
        a_service.store, "release_source_activation_marker", real_release_marker
    )
    out = await a_service.evict(pair_id, operator="op", reason="finish cleanup")
    assert out["state"] == MeshPairingState.CANCELLED.value
    assert await a_service.store.get_source_activation_marker(SPACE) is None
    await a_service.assert_no_pairing_activation(SPACE)


async def test_evict_gives_up_stuck_transferring_source(monkeypatch):
    # A source waiting at 'transferring' for a permanently-unresponsive target must
    # be givable-up through the control plane: cancel refuses (post-mutation) but
    # evict removes the admitted candidate, releases the reservation, cancels, and
    # records auditable evidence — an admitted member is never un-removable.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(107, 108)
    sess = await a_service.store.get_session(pair_id)
    assert sess.state == "transferring"  # admitted + exported, waiting for the target
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert any(m.node_id == tgt and m.status == "pending" for m in a_mem.members)

    with pytest.raises(MeshPairingServiceError):
        await a_service.cancel(pair_id)  # post-mutation: not cancellable

    out = await a_service.evict(pair_id, operator="op", reason="target unresponsive")
    assert out["state"] == "cancelled"
    a_mem2 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert not any(m.node_id == tgt and m.status in ("pending", "active") for m in a_mem2.members)
    assert await a_service.store.get_evidence(pair_id) is not None  # auditable abandonment


async def test_evict_refuses_pending_candidate_with_rewritten_incarnation() -> None:
    """A retained source pairing cannot give up a different pending candidate."""

    a_service, b_service, _a_config, b_config, a_storage, _b_storage, _peers, pair_id = (
        await _drive_to_approved(109, 110)
    )
    source_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await source_store.get_membership()
    assert membership is not None
    target_node_id = b_config.fingerprint.split(":", 1)[1]
    other_pair = "pair_ffffffffffffffffffffffffffffffff"
    await source_store.set_membership(
        membership.model_copy(
            update={
                "members": [
                    member.model_copy(update={"incarnation": other_pair})
                    if member.node_id == target_node_id
                    else member
                    for member in membership.members
                ]
            }
        )
    )
    restarted = MeshPairingService(
        a_service._config,
        a_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=a_service._sender_factory,
    )

    with pytest.raises(MeshPairingServiceError) as exc:
        await restarted.evict(pair_id, operator="op", reason="incarnation mismatch")
    assert exc.value.code == "stale_pairing"
    held = await source_store.get_membership()
    assert held is not None and held.epoch == membership.epoch
    pending = next(member for member in held.members if member.node_id == target_node_id)
    assert pending.status == "pending" and pending.incarnation == other_pair


async def test_accept_refuses_space_mismatch(monkeypatch):
    # The reserved space MUST equal the enrolled space; a mismatch is refused
    # before any reservation is taken.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, _ = await _build_admin_instances(109, 110)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    with pytest.raises(MeshPairingServiceError) as e:
        await b_service.accept_invitation(
            invite["invitation_bytes"], "othermeshspace",
            secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True,
        )
    assert e.value.code == "space_mismatch"
    assert await b_service.store.get_reservation("othermeshspace") is None  # nothing reserved
    assert await b_service.store.get_reservation(SPACE) is None


class _AckDropSender(AsgiPeerSender):
    """Delivers everything EXCEPT the target's final ACK, which it drops (503).

    The target still fetches status + bootstrap and imports (reaching
    awaiting_acks, UNSAFE, reserved), but the source never receives the ACK, so it
    stays 'transferring' with the target PENDING — the stuck-transfer give-up case
    (the target is never promoted to ACTIVE, so evict is safe)."""

    async def send(self, method, path, *, headers, body):
        if path.endswith("/ack"):
            return PeerResponse(503, [], b"")
        return await super().send(method, path, headers=headers, body=body)


async def test_evict_refuses_active_target_after_lost_activation_response(monkeypatch):
    # Response-lost-after-apply: the target APPLIES e+2 (becomes active) but its
    # signed confirmation is lost, so the source blocks. evict MUST refuse (removing
    # a live active member would split membership); resume idempotently converges.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(121, 122)

    class _ActivationResponseDrop(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            resp = await super().send(method, path, headers=headers, body=body)  # target APPLIES e+2
            if path.endswith("/events"):
                return PeerResponse(503, [], b"")  # its response is lost
            return resp

    a_service._sender_factory = lambda _e: _ActivationResponseDrop(peers, "B")  # type: ignore[attr-defined]
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] == "active"  # the TARGET applied e+2; only the source's confirmation was lost
    tgt = b_config.fingerprint.split(":", 1)[1]
    # The target genuinely applied e+2: active + HEALTHY.
    b_mem = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert b_mem.epoch == 3 and any(m.node_id == tgt and m.status == "active" for m in b_mem.members)
    b_health = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert HiveNodeStatus(b_health.status) == HiveNodeStatus.HEALTHY
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"

    # EVICT MUST REFUSE — the promoted target may be a live active member.
    with pytest.raises(MeshPairingServiceError) as e:
        await a_service.evict(pair_id, operator="op")
    assert e.value.code == "target_active"

    # RESUME converges both sides idempotently (no split-brain).
    a_service._sender_factory = lambda _e: AsgiPeerSender(peers, "B")  # type: ignore[attr-defined]
    resumed = await a_service.resume(pair_id)
    assert resumed["state"] == "active"
    assert (await a_service.store.get_session(pair_id)).state == "active"
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 3 and any(m.node_id == tgt and m.status == "active" for m in a_mem.members)


async def test_force_evict_member_removes_dead_active_target(monkeypatch):
    # If the target applied e+2 but is then genuinely dead/unreachable, resume
    # cannot converge and evict correctly refuses (target_active). The operator can
    # force ordinary member eviction to remove the dead node from the all-ACK
    # roster (so shared work is not frozen) and reconcile the pairing to terminal.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(123, 124)

    class _ActivationResponseDrop(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            resp = await super().send(method, path, headers=headers, body=body)  # target applies e+2
            if path.endswith("/events"):
                return PeerResponse(503, [], b"")
            return resp

    a_service._sender_factory = lambda _e: _ActivationResponseDrop(peers, "B")  # type: ignore[attr-defined]
    await b_service.run_target_enrollment(pair_id)  # target active; source cannot confirm
    tgt = b_config.fingerprint.split(":", 1)[1]
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 3 and any(m.node_id == tgt and m.status == "active" for m in a_mem.members)

    # The target is now DEAD/unreachable: resume cannot converge; evict refuses.
    class _Dead:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(503, [], b"")

    a_service._sender_factory = lambda _e: _Dead()  # type: ignore[attr-defined]
    resumed = await a_service.resume(pair_id)
    assert resumed["state"] == "blocked_recovery"  # cannot converge a dead target
    with pytest.raises(MeshPairingServiceError) as e:
        await a_service.evict(pair_id, operator="op")
    assert e.value.code == "target_active"

    # Force ordinary member eviction: the dead ACTIVE node leaves the roster.
    out = await a_service.force_evict_member(pair_id, operator="op", reason="node is dead")
    assert out["state"] == "cancelled" and out["evicted_node"] == tgt
    a_mem2 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem2.epoch == 4  # epoch-advancing member eviction (e+3)
    assert not any(m.node_id == tgt and m.status in ("pending", "active") for m in a_mem2.members)
    assert (await a_service.store.get_session(pair_id)).state == "cancelled"


async def test_force_evict_member_removes_dead_member_after_convergence(monkeypatch):
    # The common case: a pairing CONVERGES normally (source session active), then
    # the member later dies. evict refuses (target_active); force_evict_member
    # removes the dead ACTIVE member from the all-ACK roster (leaving the successful
    # pairing's session as its historical record) and is idempotent on retry.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(129, 130)
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] == "active"
    assert (await a_service.store.get_session(pair_id)).state == "active"  # converged
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 3 and any(m.node_id == tgt and m.status == "active" for m in a_mem.members)

    with pytest.raises(MeshPairingServiceError) as e:
        await a_service.evict(pair_id, operator="op")  # give-up refuses a promoted member
    assert e.value.code == "target_active"

    out = await a_service.force_evict_member(pair_id, operator="op", reason="node died")
    assert out["evicted_node"] == tgt and out["state"] == "active"  # session stays as history
    a_mem2 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem2.epoch == 4 and not any(m.node_id == tgt and m.status in ("pending", "active") for m in a_mem2.members)

    # Idempotent retry (member already gone): succeeds, no further epoch advance.
    out2 = await a_service.force_evict_member(pair_id, operator="op")
    assert out2["state"] == "active"
    a_mem3 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem3.epoch == 4


async def test_concurrent_approvals_admit_exactly_one(monkeypatch):
    # Two invitations from the same single-member source share base_epoch. Two
    # concurrent approvals must NOT both admit (which would strand both bootstraps
    # at successive epochs): the space lock serializes them, so exactly one admits
    # and the other fails its epoch check.
    import asyncio

    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([141]) * 32).decode().rstrip("="), A_URL)
    b_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([142]) * 32).decode().rstrip("="), B_URL)
    c_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([143]) * 32).decode().rstrip("="), "https://c.mesh.test")
    a_storage, b_storage, c_storage = FakeStorage(), FakeStorage(), FakeStorage()
    await _seed_source(a_storage, a_config)
    await _seed_blank_target(b_storage)
    await _seed_blank_target(c_storage)
    clock = lambda: NOW_MS
    peers: dict = {}
    a_service = MeshPairingService(a_config, a_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    b_service = MeshPairingService(b_config, b_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    c_service = MeshPairingService(c_config, c_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    a_router = MeshNamespaceRouter(_fallback(), config=a_config, process_lock=FakeProcessLock(), storage_factory=lambda: a_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=a_service)
    b_router = MeshNamespaceRouter(_fallback(), config=b_config, process_lock=FakeProcessLock(), storage_factory=lambda: b_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=b_service)
    c_router = MeshNamespaceRouter(_fallback(), config=c_config, process_lock=FakeProcessLock(), storage_factory=lambda: c_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=c_service)
    peers["A"], peers["B"], peers["C"] = a_router, b_router, c_router

    inv1 = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    inv2 = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    await b_service.accept_invitation(inv1["invitation_bytes"], SPACE, secret=inv1["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)
    await c_service.accept_invitation(inv2["invitation_bytes"], SPACE, secret=inv2["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)

    # FORCE interleaving: park the FIRST approval AFTER its epoch check (it persists
    # the 'approved' session, past the check, before admit) so the second approval
    # runs concurrently. WITHOUT the space lock the second would pass its stale epoch
    # check here and both would admit (2 pending); WITH it the second blocks on the
    # space lock and only re-reads the bumped epoch after the first fully admits.
    gate = asyncio.Event()
    real_put = a_service.store.put_session
    parked = {"done": False}

    async def gated_put(session):
        if session.role == "source" and session.state == "approved" and not parked["done"]:
            parked["done"] = True
            await gate.wait()
        return await real_put(session)

    a_service.store.put_session = gated_put  # type: ignore[method-assign]
    task = asyncio.gather(
        a_service.approve(inv1["pair_id"]),
        a_service.approve(inv2["pair_id"]),
        return_exceptions=True,
    )
    for _ in range(20):  # let both approvals reach their parking point
        await asyncio.sleep(0)
    gate.set()
    results = await task
    a_service.store.put_session = real_put  # type: ignore[method-assign]

    ok = [r for r in results if isinstance(r, dict)]
    errs = [r for r in results if isinstance(r, MeshPairingServiceError)]
    assert len(ok) == 1 and len(errs) == 1, results
    assert errs[0].code == "epoch_changed"

    # The source admitted EXACTLY ONE pending target (not two) at e+1.
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 2
    pending = [m for m in a_mem.members if m.status == "pending"]
    assert len(pending) == 1


async def test_second_pairing_refused_while_first_candidate_pending(monkeypatch):
    """Single-in-flight gate (self-review #2): once one pairing has admitted a
    PENDING candidate, approving a SECOND pairing on the same source is refused —
    so two pairings can never both go in-flight and mutually fence each other's
    activation/give-up (a liveness wedge). The first pairing still converges, and
    after it fully evicts, a fresh pairing is allowed again."""

    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([171]) * 32).decode().rstrip("="), A_URL)
    b_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([172]) * 32).decode().rstrip("="), B_URL)
    c_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([173]) * 32).decode().rstrip("="), "https://c.mesh.test")
    a_storage, b_storage, c_storage = FakeStorage(), FakeStorage(), FakeStorage()
    await _seed_source(a_storage, a_config)
    await _seed_blank_target(b_storage)
    await _seed_blank_target(c_storage)
    clock = lambda: NOW_MS
    peers: dict = {}
    a_service = MeshPairingService(a_config, a_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    b_service = MeshPairingService(b_config, b_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    c_service = MeshPairingService(c_config, c_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    for cfg, st, key in ((a_config, a_storage, "A"), (b_config, b_storage, "B"), (c_config, c_storage, "C")):
        peers[key] = MeshNamespaceRouter(_fallback(), config=cfg, process_lock=FakeProcessLock(), storage_factory=lambda st=st: st, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service={"A": a_service, "B": b_service, "C": c_service}[key])

    inv1 = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    await b_service.accept_invitation(inv1["invitation_bytes"], SPACE, secret=inv1["secret"], source_endpoint=A_URL, requested_scopes=("read",), quiesced=True)
    await a_service.approve(inv1["pair_id"])  # pairing 1 admits its target PENDING

    # A SECOND invitation minted AFTER the first admitted has a fresh base_epoch, so
    # it passes the epoch check — the PENDING-candidate gate must still refuse it.
    inv2 = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    await c_service.accept_invitation(inv2["invitation_bytes"], SPACE, secret=inv2["secret"], source_endpoint=A_URL, requested_scopes=("read",), quiesced=True)
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.approve(inv2["pair_id"])
    assert exc.value.code == "pairing_in_flight"

    # Exactly one PENDING candidate exists; the second pairing never admitted.
    mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert len([m for m in mem.members if m.status == "pending"]) == 1

    # Give up pairing 1; a fresh pairing is then allowed (gate clears).
    await a_service.evict(inv1["pair_id"], operator="op")
    inv3 = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    assert inv3["pair_id"]  # no longer refused


async def test_approve_vs_concurrent_rescope_fails_closed(monkeypatch):
    # A membership mutation (re-scope) concurrent with approve must not corrupt the
    # epoch fence: the compare-and-admit (under the membership lock) admits ONLY at
    # base_epoch, so a rescope that advances the epoch before admission fails the
    # approval closed (epoch_changed) rather than admitting at the wrong epoch and
    # exporting a snapshot the target rejects.
    import asyncio

    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, _ = await _build_admin_instances(151, 152)
    inv = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id = inv["pair_id"]
    await b_service.accept_invitation(inv["invitation_bytes"], SPACE, secret=inv["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"), quiesced=True)

    # Queue a real rescope before approve on the shared membership lock. Approve
    # may complete its early read while both wait, but the rescope wins the lock
    # and advances base(1) -> 2 before approve's under-lock deep proof.
    membership = a_service._membership(SPACE)
    lock = membership.space_lock()
    await lock.acquire()
    rescope = asyncio.create_task(
        membership.update_member_scopes(
            "sourcenode0000000000000000000000", ["read"]
        )
    )
    await asyncio.sleep(0)
    task = asyncio.create_task(a_service.approve(pair_id))
    await asyncio.sleep(0)
    lock.release()
    await rescope
    with pytest.raises(MeshPairingServiceError) as e:
        await task
    assert e.value.code == "epoch_changed"

    # No approval/session mutation happened at the wrong epoch: only the
    # rescope's bump, and the claim remains retryable evidence.
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 2
    assert not any(m.status == "pending" for m in a_mem.members)
    session = await a_service.store.get_session(pair_id)
    assert session is not None and session.state == "claimed"
    assert await a_service.store.get_blob(pair_id, "approval") is None


async def test_approval_capacity_boundary_covers_max_width_timestamps(monkeypatch):
    """The e+1 proof is an upper bound even across isoformat's exact-second case."""

    from dataclasses import replace

    from live_mem.mesh.bootstrap_snapshot import serialize_snapshot
    from live_mem.mesh.pairing_service import (
        MeshPairingServiceError,
        _legacy_membership_key,
        _node_id_from_fingerprint,
    )

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        _pair_id,
    ) = await _build_admin_instances(153, 154)
    invitation = await a_service.create_invitation(
        SPACE, requested_scopes=("read", "commit")
    )
    pair_id = invitation["pair_id"]
    await b_service.accept_invitation(
        invitation["invitation_bytes"],
        SPACE,
        secret=invitation["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read", "commit"), quiesced=True,
    )
    session = await a_service.store.get_session(pair_id)
    assert session is not None and session.state == "claimed"
    store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    node = await store.get_node_identity()
    term = await store.get_term()
    assert node is not None and term is not None
    candidate = Member(
        node_id=_node_id_from_fingerprint(session.target_fingerprint),
        public_key=_legacy_membership_key(session.target_public_key),
        scopes=list(session.granted_scopes),
        incarnation=pair_id,
    )
    current = await a_service._deep_source_bootstrap_preflight(SPACE)
    projected = a_service._bootstrap().project_membership_admission_snapshot(
        current,
        space_id=SPACE,
        candidate=candidate,
        source_node=node,
        term=term,
    )
    capacity = len(serialize_snapshot(projected))
    assert projected.manifest.created_at.endswith(".000000+00:00")
    assert ".000000+00:00" in projected.files["_hivemind/members.json"]
    assert any(".000000+00-00" in path for path in projected.files)

    # One byte below the conservative exact e+1 bound refuses before approval
    # evidence or shared membership changes, leaving CLAIMED retryable.
    a_service._config = replace(
        a_service._config, bootstrap_max_bytes=capacity - 1
    )
    membership_before = await store.get_membership()
    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.approve(pair_id)
    assert exc.value.code == "bootstrap_limit_exceeded"
    assert await a_service.store.get_blob(pair_id, "approval") is None
    assert (await a_service.store.get_session(pair_id)).state == "claimed"
    assert await store.get_membership() == membership_before

    # At the projected bound the real timestamps are never wider, so admission
    # and the subsequent exact export succeed without a post-e+1 limit failure.
    a_service._config = replace(a_service._config, bootstrap_max_bytes=capacity)
    result = await a_service.approve(pair_id)
    assert result["state"] == "transferring"
    payload = await a_service.store.get_blob(pair_id, "bootstrap_payload")
    assert payload is not None and len(payload) <= capacity


async def test_approval_capacity_retry_reuses_orphaned_deterministic_event() -> None:
    """An event-first crash retry must not reserve capacity for a duplicate."""

    import uuid
    from dataclasses import replace

    from live_mem.core.hivemind import EventEnvelope, EventType
    from live_mem.mesh.pairing_service import (
        _legacy_membership_key,
        _node_id_from_fingerprint,
    )

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        _pair_id,
    ) = await _build_admin_instances(155, 156)
    invitation = await a_service.create_invitation(
        SPACE, requested_scopes=("read", "commit")
    )
    pair_id = invitation["pair_id"]
    await b_service.accept_invitation(
        invitation["invitation_bytes"],
        SPACE,
        secret=invitation["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read", "commit"), quiesced=True,
    )
    session = await a_service.store.get_session(pair_id)
    assert session is not None
    store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await store.get_membership()
    node = await store.get_node_identity()
    term = await store.get_term()
    assert membership is not None and node is not None and term is not None
    candidate = Member(
        node_id=_node_id_from_fingerprint(session.target_fingerprint),
        public_key=_legacy_membership_key(session.target_public_key),
        scopes=list(session.granted_scopes),
        incarnation=pair_id,
    )
    new_epoch = membership.epoch + 1
    event_id = uuid.uuid5(
        uuid.NAMESPACE_OID,
        f"{SPACE}:{EventType.MEMBERSHIP_UPDATED.value}:"
        f"{candidate.node_id}:{new_epoch}",
    ).hex
    await store.append_event(
        EventEnvelope(
            event_id=event_id,
            type=EventType.MEMBERSHIP_UPDATED,
            origin_node_id=node.node_id,
            term=term.term,
            membership_epoch=new_epoch,
            created_at="2026-08-19T12:00:00.123456+00:00",
            payload={
                "node_id": candidate.node_id,
                "epoch": new_epoch,
                "status": "pending",
            },
        )
    )

    current = await a_service._deep_source_bootstrap_preflight(SPACE)
    projected = a_service._bootstrap().project_membership_admission_snapshot(
        current,
        space_id=SPACE,
        candidate=candidate,
        source_node=node,
        term=term,
    )
    suffix = f"_{event_id}.json"
    assert len([path for path in projected.files if path.endswith(suffix)]) == 1
    assert len(projected.files) == len(current.files)

    a_service._config = replace(
        a_service._config,
        bootstrap_max_objects=len(current.files),
    )
    result = await a_service.approve(pair_id)
    assert result["state"] == "transferring"


async def test_bootstrap_export_strips_member_incarnation(monkeypatch):
    # The source tags its admitted member with a source-local incarnation, but the
    # EXPORTED bootstrap members.json must NOT carry it — the shape stays identical
    # to a pre-P10-3 reader (extra='forbid'), and convergence is unaffected.
    from live_mem.core.hivemind import MembershipView

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(135, 136)
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert next(m for m in a_mem.members if m.node_id == tgt).incarnation == pair_id  # source-local tag

    snapshot = await a_service._bootstrap().export_snapshot(SPACE)
    members_json = snapshot.files["_hivemind/members.json"]
    assert "incarnation" not in members_json  # stripped from the shared export
    exported = MembershipView.model_validate_json(members_json)
    assert all(m.incarnation is None for m in exported.members)


async def test_force_evict_member_fails_closed_on_missing_membership(monkeypatch):
    # A missing members.json is critical-state loss, NOT an idempotent eviction
    # retry (a real evict preserves an EVICTED record). force_evict_member must
    # fail closed rather than falsely reporting recovery over lost membership.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(131, 132)
    assert (await b_service.run_target_enrollment(pair_id))["state"] == "active"
    await a_storage.delete(f"{SPACE}/_hivemind/members.json")  # membership lost/corrupt
    with pytest.raises(MeshPairingServiceError) as e:
        await a_service.force_evict_member(pair_id, operator="op")
    assert e.value.code == "membership_unavailable"


async def test_force_evict_stale_pairing_refuses_after_re_enrollment(monkeypatch):
    # A retained 'active' pairing must NOT authorize eviction of a LATER
    # re-enrollment of the same identity: force_evict is bound to the member
    # incarnation this pairing admitted (Member.incarnation == pair_id), compared
    # atomically under the membership lock. A re-admission carries a fresh pair_id,
    # so the stale pairing is refused and the healthy re-enrolled member is untouched.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id1 = await _drive_to_approved(133, 134)
    assert (await b_service.run_target_enrollment(pair_id1))["state"] == "active"
    tgt = b_config.fingerprint.split(":", 1)[1]

    await a_service.force_evict_member(pair_id1, operator="op", reason="dead")  # P1 evicts B; session stays active

    # B re-enrolls with the SAME identity only after its forced-eviction
    # decommission has replaced the entire dead target instance, including all
    # local pairing metadata.  Teardown of the shared target *space* alone is
    # deliberately insufficient: it must not erase a signed terminal tail from
    # a potentially live source member.  Model the documented dead-node rebuild
    # with a clean local store/router, rather than selectively deleting fence
    # keys (which would teach an unsafe production recovery shortcut).
    rebuilt_storage = FakeStorage()
    await _seed_blank_target(rebuilt_storage)
    rebuilt_service = MeshPairingService(
        b_config,
        rebuilt_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=lambda _e: AsgiPeerSender(peers, "A"),
    )
    peers["B"] = MeshNamespaceRouter(
        _fallback(),
        config=b_config,
        process_lock=FakeProcessLock(),
        storage_factory=lambda: rebuilt_storage,
        replay_ledger=FakeReplayLedger(),
        clock_ms=lambda: NOW_MS,
        pairing_service=rebuilt_service,
    )
    b_service = rebuilt_service
    b_storage = rebuilt_storage
    invite2 = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id2 = invite2["pair_id"]
    await b_service.accept_invitation(
        invite2["invitation_bytes"], SPACE, secret=invite2["secret"], source_endpoint=A_URL,
        requested_scopes=("read", "commit"), quiesced=True,
    )
    await a_service.approve(pair_id2)
    assert (await b_service.run_target_enrollment(pair_id2))["state"] == "active"
    a_mem2 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert any(m.node_id == tgt and m.status == "active" for m in a_mem2.members)  # re-enrolled B is live

    # The STALE P1 force_evict must REFUSE (newer incarnation).
    with pytest.raises(MeshPairingServiceError) as e:
        await a_service.force_evict_member(pair_id1, operator="op")
    assert e.value.code == "stale_pairing"
    a_mem3 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert any(m.node_id == tgt and m.status == "active" for m in a_mem3.members)  # untouched


async def test_awaiting_acks_crash_dead_target_force_evict(monkeypatch):
    # The crash window between promote (membership ACTIVE) and the active/blocked
    # persist leaves the source durably 'awaiting_acks' with the target ACTIVE. If
    # that target is dead, resume must persist blocked_recovery (honest durable
    # state) so force_evict_member — which gates on blocked_recovery — can remove
    # the dead node from the all-ACK roster. No ACTIVE-target state is unrecoverable.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(125, 126)

    async def boom(*a, **k):
        raise RuntimeError("crash after promote, before persisting active/blocked")

    monkeypatch.setattr(a_service, "_complete_source_activation_tail", boom)
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    a_sess = await a_service.store.get_session(pair_id)
    assert a_sess.state == "awaiting_acks"  # crash window: promoted but not persisted active
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 3 and any(m.node_id == tgt and m.status == "active" for m in a_mem.members)

    monkeypatch.undo()  # restore terminal activation delivery

    class _Dead:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(503, [], b"")

    a_service._sender_factory = lambda _e: _Dead()  # type: ignore[attr-defined]  # target dead
    resumed = await a_service.resume(pair_id)
    assert resumed["state"] == "blocked_recovery"
    # Durable state is NOW blocked_recovery (matches the response), so it is forcible.
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"
    with pytest.raises(MeshPairingServiceError) as e:
        await a_service.evict(pair_id, operator="op")
    assert e.value.code == "target_active"

    out = await a_service.force_evict_member(pair_id, operator="op", reason="node is dead")
    assert out["state"] == "cancelled"
    a_mem2 = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem2.epoch == 4  # epoch-advancing member eviction
    assert not any(m.node_id == tgt and m.status in ("pending", "active") for m in a_mem2.members)


async def test_force_evict_retry_finishes_cancelled_marker_release(monkeypatch):
    """The same force-evict retry finishes its durable post-eviction tail."""

    a_service, b_service, _a_config, b_config, a_storage, _b_storage, peers, pair_id = (
        await _drive_to_approved(127, 128)
    )

    class _ActivationResponseDrop(AsgiPeerSender):
        async def send(self, method, path, *, headers, body):
            response = await super().send(method, path, headers=headers, body=body)
            if path.endswith("/events"):
                return PeerResponse(503, [], b"")
            return response

    a_service._sender_factory = lambda _endpoint: _ActivationResponseDrop(  # type: ignore[method-assign]
        peers, "B"
    )
    await b_service.run_target_enrollment(pair_id)
    assert (await a_service.store.get_session(pair_id)).state == "blocked_recovery"

    class _HardCrash(BaseException):
        pass

    real_release_marker = a_service.store.release_source_activation_marker

    async def crash_before_marker_release(space_id: str, owner_pair_id: str) -> None:
        assert space_id == SPACE and owner_pair_id == pair_id
        raise _HardCrash("crash after force-eviction cancelled write")

    monkeypatch.setattr(
        a_service.store,
        "release_source_activation_marker",
        crash_before_marker_release,
    )
    with pytest.raises(_HardCrash):
        await a_service.force_evict_member(pair_id, operator="op", reason="dead")

    session = await a_service.store.get_session(pair_id)
    assert session is not None and session.state == MeshPairingState.CANCELLED.value
    marker = await a_service.store.get_source_activation_marker(SPACE)
    assert marker is not None and marker.evidence.pair_id == pair_id
    target_id = b_config.fingerprint.split(":", 1)[1]
    membership = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_membership()
    assert membership is not None
    assert any(
        member.node_id == target_id
        and member.status == "evicted"
        and member.incarnation == pair_id
        for member in membership.members
    )

    monkeypatch.setattr(
        a_service.store, "release_source_activation_marker", real_release_marker
    )
    restarted = MeshPairingService(
        a_service._config,
        a_storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=a_service._sender_factory,
    )
    out = await restarted.force_evict_member(pair_id, operator="op", reason="retry")
    assert out["state"] == MeshPairingState.CANCELLED.value
    assert await restarted.store.get_source_activation_marker(SPACE) is None
    await restarted.assert_no_pairing_activation(SPACE)


async def test_source_evict_then_target_abandon_releases_reservation(monkeypatch):
    # On separate instances the reservation is target-owned, so a source evict
    # cannot release it. After the source gives up, the target must be able to
    # abandon: verify the source's signed cancellation, release its OWN reservation,
    # tear its imported space back to blank, and cancel — never left UNSAFE+reserved.
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(111, 112)

    b_service._sender_factory = lambda _e: _AckDropSender(peers, "A")  # type: ignore[attr-defined]  # ACK lost
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    # The source never got the ACK, so it stays 'transferring' with the target PENDING.
    assert (await a_service.store.get_session(pair_id)).state == "transferring"
    b_stuck = await b_service.store.get_session(pair_id)
    assert b_stuck.state == "awaiting_acks"  # target imported (UNSAFE) + reserved
    assert await b_service.store.get_reservation(SPACE) == pair_id
    b_health = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_node_status()  # type: ignore[arg-type]
    assert HiveNodeStatus(b_health.status) == HiveNodeStatus.UNSAFE

    await a_service.evict(pair_id, operator="op", reason="give up")  # target PENDING -> safe
    assert (await a_service.store.get_session(pair_id)).state == "cancelled"
    # The source evict did NOT (could not) release the target's reservation.
    assert await b_service.store.get_reservation(SPACE) == pair_id

    out = await b_service.abandon(pair_id)
    assert out["state"] == "cancelled"
    assert await b_service.store.get_reservation(SPACE) is None  # target released its own
    await b_service._bootstrap()._assert_blank_target(SPACE)  # torn back to blank (reusable)


async def test_abandon_with_lost_raw_reservation_tears_down_before_fence_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost raw reservation cannot turn an imported target into a bare cancel.

    The signed #417 direct fence and immutable acceptance intent still bind the
    target.  After the source's signed eviction, abandon must use that proof to
    reset the imported target to blank before it releases the fence; a later
    ``cancel`` retry must not open ordinary writes over retained import data.
    """

    a_service, b_service, _a_config, _b_config, _a_storage, _b_storage, peers, pair_id = (
        await _drive_to_approved(113, 114)
    )
    b_service._sender_factory = lambda _e: _AckDropSender(peers, "A")  # type: ignore[attr-defined]
    await b_service.run_target_enrollment(pair_id)
    await a_service.evict(pair_id, operator="op", reason="give up")

    # Model a durable reservation-object loss after the target already imported
    # e+1 and marked itself UNSAFE. The target #417 authority remains intact.
    await b_service.store.release(SPACE, pair_id)
    assert await b_service.store.get_reservation(SPACE) is None
    fence = await b_service.store.get_target_pairing_fence(SPACE)
    assert fence is not None and fence.authority.phase == "held"

    out = await b_service.abandon(pair_id)
    assert out["state"] == "cancelled"
    await b_service._bootstrap()._assert_blank_target(SPACE)
    released = await b_service.store.get_target_pairing_fence(SPACE)
    assert released is not None and released.authority.phase == "released"
    await b_service.cancel(pair_id)  # exact terminal retry is now harmless
    await b_service.assert_space_not_reserved(SPACE)


async def test_abandon_skips_teardown_when_space_no_longer_owned(monkeypatch):
    # A stale re-abandon must NOT destroy a re-paired space's live data: teardown is
    # gated on THIS pairing still owning the reservation (guards the crash window
    # between release and the cancel transition + a subsequent re-pair).
    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(115, 116)

    b_service._sender_factory = lambda _e: _AckDropSender(peers, "A")  # type: ignore[attr-defined]
    await b_service.run_target_enrollment(pair_id)  # target imports, stuck awaiting_acks
    await a_service.evict(pair_id, operator="op")  # source gives up (target PENDING)

    # Simulate a partial-abandon crash + re-pair: release P1's reservation, hand the
    # space to a DIFFERENT pairing, and write 'live' data into it.
    await b_service.store.release(SPACE, pair_id)
    other_pair = "pair_" + "b" * 32
    await b_service.store.reserve(SPACE, other_pair, now_ms=NOW_MS)
    await b_storage.put(f"{SPACE}/_hivemind/members.json", '{"epoch":9,"members":[]}')

    out = await b_service.abandon(pair_id)  # stale abandon of the OLD pairing
    assert out["state"] == "cancelled"
    # The re-paired space's reservation AND live data are untouched.
    assert await b_service.store.get_reservation(SPACE) == other_pair
    assert await b_storage.get(f"{SPACE}/_hivemind/members.json") is not None


async def test_abandon_teardown_is_serialized_under_space_lock(monkeypatch):
    # The ownership re-check + teardown + release are atomic under the space lock
    # (the same lock accept holds across reserve), so no concurrent re-pair can
    # interleave between the check and the teardown (check-to-teardown TOCTOU).
    import asyncio

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(117, 118)

    b_service._sender_factory = lambda _e: _AckDropSender(peers, "A")  # type: ignore[attr-defined]
    await b_service.run_target_enrollment(pair_id)
    await a_service.evict(pair_id, operator="op")

    # Hold the target's space lock so abandon cannot reach its teardown.
    space_lock = b_service.store.space_lock(SPACE)
    await space_lock.acquire()
    task = asyncio.ensure_future(b_service.abandon(pair_id))
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.2)  # blocked on the lock
        assert await b_service.store.get_reservation(SPACE) == pair_id  # not torn down yet
    finally:
        space_lock.release()
    out = await task
    assert out["state"] == "cancelled"
    assert await b_service.store.get_reservation(SPACE) is None  # released once the lock frees


async def test_abandon_rejects_forged_source_status(monkeypatch):
    # A forged/unsigned source status claiming 'cancelled' must NOT be trusted:
    # abandon requires a verifiable signed source response before tearing down.
    import json as _json

    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(119, 120)

    class _ForgedCancelled:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(200, [], _json.dumps({"state": "cancelled"}).encode())

    b_service._sender_factory = lambda _e: _ForgedCancelled()  # type: ignore[attr-defined]
    with pytest.raises(MeshPairingServiceError) as e:
        await b_service.abandon(pair_id)
    # Unsigned/unverifiable response -> not trusted (bad_response from the signed-
    # envelope check, or source_unverified from the generic guard).
    assert e.value.code in ("bad_response", "source_unverified")
    assert await b_service.store.get_reservation(SPACE) == pair_id  # untouched


async def test_abandon_refuses_while_source_still_enrolling(monkeypatch):
    # A target must NOT tear itself down while the source is still enrolling it
    # (the pairing may still converge) — abandon is fail-closed on the source state.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(113, 114)
    # Source is 'transferring' (actively enrolling); the target is 'claimed'.
    with pytest.raises(MeshPairingServiceError) as e:
        await b_service.abandon(pair_id)
    assert e.value.code == "source_still_enrolling"
    assert await b_service.store.get_reservation(SPACE) == pair_id  # untouched


async def test_bootstrap_envelope_must_bind_to_session(monkeypatch):
    # A source-validly-signed bootstrap envelope for a DIFFERENT target must be
    # rejected on binding — a valid signature is not consent to import anything.
    from live_mem.mesh.bootstrap_snapshot import build_bootstrap
    from live_mem.mesh.identity import generate_mesh_identity
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(105, 106)
    snapshot = await a_service._bootstrap().export_snapshot(SPACE)
    wrong_target_fp = generate_mesh_identity().fingerprint
    signed_env, payload = build_bootstrap(
        snapshot,
        space_id=SPACE,
        source_public_key=a_config.public_key,
        source_fingerprint=a_config.fingerprint,
        target_fingerprint=wrong_target_fp,  # addressed to a DIFFERENT target
        private_key=a_config.private_key,
    )
    # Plant the (validly-signed) mis-bound envelope as the source's stored blob.
    await a_service.store.put_blob(pair_id, "bootstrap_envelope", signed_env.canonical_bytes())
    await a_service.store.put_blob(pair_id, "bootstrap_payload", payload)

    with pytest.raises(MeshPairingServiceError) as e:
        await b_service.run_target_enrollment(pair_id)
    assert e.value.code == "bad_binding"


async def _build_admin_instances(a_seed, b_seed):
    a_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([a_seed]) * 32).decode().rstrip("="), A_URL)
    b_config = _config(MESH_PRIVATE_KEY_PREFIX + base64.urlsafe_b64encode(bytes([b_seed]) * 32).decode().rstrip("="), B_URL)
    a_storage, b_storage = FakeStorage(), FakeStorage()
    await _seed_source(a_storage, a_config)
    await _seed_blank_target(b_storage)
    clock = lambda: NOW_MS
    peers: dict = {}
    a_service = MeshPairingService(a_config, a_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "B"))
    b_service = MeshPairingService(b_config, b_storage, clock_ms=clock, sender_factory=lambda _e: AsgiPeerSender(peers, "A"))
    a_router = MeshNamespaceRouter(_fallback(), config=a_config, process_lock=FakeProcessLock(), storage_factory=lambda: a_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=a_service)
    b_router = MeshNamespaceRouter(_fallback(), config=b_config, process_lock=FakeProcessLock(), storage_factory=lambda: b_storage, replay_ledger=FakeReplayLedger(), clock_ms=clock, pairing_service=b_service)
    peers["A"], peers["B"] = a_router, b_router
    return a_service, b_service, a_config, b_config, a_storage, b_storage, peers, None


async def test_activation_fence_blocks_rescope_between_promotion_and_delivery(monkeypatch):
    """Post-promotion delivery window (the round-14 finding): a concurrent operator
    rescope INJECTED after the atomic e+1 -> e+2 promotion and BEFORE the target
    confirms activation must be REFUSED by the pairing-activation fence, so the
    source cannot advance to e+3 while the target self-promotes to the pre-computed
    e+2 (which would split the two MembershipViews). Once the pairing converges the
    fence clears and an operator rescope proceeds normally."""

    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(151, 152)
    source_node = "sourcenode0000000000000000000000"
    outcomes: list[str] = []

    class _RescopeAtActivation(AsgiPeerSender):
        """Injects ONE operator rescope at the activation-delivery POST — i.e. after
        the promotion committed (membership already e+2, session AWAITING_ACKS, the
        membership lock released) and before the target confirms."""

        def __init__(self, peers, key) -> None:
            super().__init__(peers, key)
            self._fired = False

        async def send(self, method, path, *, headers, body):
            if not self._fired and path == "/mesh/v1/events":
                self._fired = True
                try:
                    await a_service._membership(SPACE).update_member_scopes(source_node, ["read"])
                    outcomes.append("not-fenced")
                except PairingActivationError:
                    outcomes.append("fenced")
                mid = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
                outcomes.append(f"epoch={mid.epoch}")
            return await super().send(method, path, headers=headers, body=body)

    register_pairing_activation_checker(a_service.assert_no_pairing_activation)
    try:
        sender = _RescopeAtActivation(peers, "B")
        a_service._sender_factory = lambda _e: sender  # type: ignore[attr-defined]
        result = await b_service.run_target_enrollment(pair_id)
    finally:
        clear_pairing_activation_checker()

    # The rescope fired mid-activation, was refused, and left the epoch at e+2.
    assert outcomes == ["fenced", "epoch=3"]
    assert result["state"] == "active"
    # No split: both sides land on the SAME epoch with the target ACTIVE.
    a_conv = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    b_conv = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_conv.epoch == 3 and b_conv.epoch == 3
    tgt = b_config.fingerprint.split(":", 1)[1]
    assert any(m.node_id == tgt and m.status == "active" for m in a_conv.members)
    assert any(m.node_id == tgt and m.status == "active" for m in b_conv.members)

    # Fence cleared after convergence: an operator rescope now advances the epoch.
    register_pairing_activation_checker(a_service.assert_no_pairing_activation)
    try:
        await a_service._membership(SPACE).update_member_scopes(source_node, ["read"])
    finally:
        clear_pairing_activation_checker()
    a_after = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_after.epoch == 4


async def test_activation_fence_blocks_external_admit_between_promotion_and_delivery(monkeypatch):
    """Caller-bound proof (Codex round-15): an EXTERNAL admit_pending_candidate for
    ANOTHER node, injected after promotion and before delivery, must be REFUSED —
    otherwise it advances the source to e+3 while the target self-promotes to the
    precomputed e+2. The pairing's OWN promote (which passes its pair_id) still
    converges."""

    from live_mem.core.hivemind import generate_peer_keypair
    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(155, 156)
    outcomes: list[str] = []

    class _AdmitAtActivation(AsgiPeerSender):
        def __init__(self, peers, key) -> None:
            super().__init__(peers, key)
            self._fired = False

        async def send(self, method, path, *, headers, body):
            if not self._fired and path == "/mesh/v1/events":
                self._fired = True
                try:
                    await a_service._membership(SPACE).admit_pending_candidate(
                        Member(node_id="intruder000000000000000000000000",
                               public_key=generate_peer_keypair().public_key)
                    )
                    outcomes.append("not-fenced")
                except PairingActivationError:
                    outcomes.append("fenced")
                mid = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
                outcomes.append(f"epoch={mid.epoch}")
            return await super().send(method, path, headers=headers, body=body)

    register_pairing_activation_checker(a_service.assert_no_pairing_activation)
    try:
        sender = _AdmitAtActivation(peers, "B")
        a_service._sender_factory = lambda _e: sender  # type: ignore[attr-defined]
        result = await b_service.run_target_enrollment(pair_id)
    finally:
        clear_pairing_activation_checker()

    assert outcomes == ["fenced", "epoch=3"]  # external admit refused; still e+2
    assert result["state"] == "active"
    a_conv = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    b_conv = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_conv.epoch == 3 and b_conv.epoch == 3  # no split, no intruder
    assert not any(m.node_id == "intruder000000000000000000000000" for m in a_conv.members)
    tgt = b_config.fingerprint.split(":", 1)[1]
    assert any(m.node_id == tgt and m.status == "active" for m in a_conv.members)
    assert any(m.node_id == tgt and m.status == "active" for m in b_conv.members)


async def test_activation_fence_blocks_external_evict_between_promotion_and_delivery(monkeypatch):
    """Caller-bound proof (Codex round-15): a DIRECT evict_member of the target,
    injected after promotion and before delivery, must be REFUSED — otherwise the
    source drops the target at e+3 while the stale e+2 activation self-promotes it.
    An external caller passes no pairing bypass, so it is fenced."""

    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(157, 158)
    tgt = b_config.fingerprint.split(":", 1)[1]
    outcomes: list[str] = []

    class _EvictAtActivation(AsgiPeerSender):
        def __init__(self, peers, key) -> None:
            super().__init__(peers, key)
            self._fired = False

        async def send(self, method, path, *, headers, body):
            if not self._fired and path == "/mesh/v1/events":
                self._fired = True
                try:
                    await a_service._membership(SPACE).evict_member(
                        tgt, operator="op", confirm=True
                    )
                    outcomes.append("not-fenced")
                except PairingActivationError:
                    outcomes.append("fenced")
                mid = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
                m = next((x for x in mid.members if x.node_id == tgt), None)
                outcomes.append(f"epoch={mid.epoch}:{m.status if m else 'absent'}")
            return await super().send(method, path, headers=headers, body=body)

    register_pairing_activation_checker(a_service.assert_no_pairing_activation)
    try:
        sender = _EvictAtActivation(peers, "B")
        a_service._sender_factory = lambda _e: sender  # type: ignore[attr-defined]
        result = await b_service.run_target_enrollment(pair_id)
    finally:
        clear_pairing_activation_checker()

    # Evict refused mid-activation; the target stayed ACTIVE at e+2 (not dropped).
    assert outcomes == ["fenced", "epoch=3:active"]
    assert result["state"] == "active"
    a_conv = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    b_conv = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_conv.epoch == 3 and b_conv.epoch == 3  # no split
    assert any(m.node_id == tgt and m.status == "active" for m in a_conv.members)
    assert any(m.node_id == tgt and m.status == "active" for m in b_conv.members)


async def test_activation_fence_blocks_rescope_during_blocked_recovery_then_resume(monkeypatch):
    """A promoted-but-unconfirmed pairing sits in blocked_recovery with
    next_action=resume (membership already at e+2, target stranded pending). An
    operator rescope during that window is refused by the fence — otherwise a later
    resume would re-drive the fixed e+2 activation against a roster the operator had
    advanced. resume converges and the fence then clears."""

    from live_mem.core.reservation_guard import (
        PairingActivationError,
        clear_pairing_activation_checker,
        register_pairing_activation_checker,
    )

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(153, 154)
    source_node = "sourcenode0000000000000000000000"

    # Lose the activation delivery so the source blocks (promoted at e+2, target
    # stranded pending, signed evidence next_action=resume).
    class _BlackHole:
        async def send(self, method, path, *, headers, body):
            return PeerResponse(503, [], b"")

    a_service._sender_factory = lambda _e: _BlackHole()  # type: ignore[attr-defined]
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    blocked = await a_service.store.get_session(pair_id)
    assert blocked.state == "blocked_recovery"

    register_pairing_activation_checker(a_service.assert_no_pairing_activation)
    try:
        with pytest.raises(PairingActivationError):
            await a_service._membership(SPACE).update_member_scopes(source_node, ["read"])
        # Refused before any mutation: the epoch is untouched at e+2.
        held = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
        assert held.epoch == 3
        # Resume idempotently re-drives the SAME e+2 activation and converges.
        a_service._sender_factory = lambda _e: AsgiPeerSender(peers, "B")  # type: ignore[attr-defined]
        resumed = await a_service.resume(pair_id)
        assert resumed["state"] == "active"
        # Fence cleared: an operator rescope is now allowed and advances the epoch.
        await a_service._membership(SPACE).update_member_scopes(source_node, ["read"])
    finally:
        clear_pairing_activation_checker()
    after = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert after.epoch == 4
    tgt = b_config.fingerprint.split(":", 1)[1]
    b_conv = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert any(m.node_id == tgt and m.status == "active" for m in b_conv.members)


async def test_resume_refuses_when_epoch_reached_e2_without_our_promotion(monkeypatch):
    """Defense-in-depth: if the pre-awaiting_acks race let a rescope advance the
    epoch to e+2 while THIS pairing's promotion was refused (target still PENDING),
    resume must NOT re-drive the fixed e+2 activation (which would self-promote the
    target against a roster the operator advanced) — it fails closed."""

    from live_mem.mesh.pairing_service import MeshPairingServiceError
    from live_mem.mesh.pairing_state import MeshPairingState

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(161, 162)
    source_node = "sourcenode0000000000000000000000"

    # Simulate the pre-arm race outcome: a rescope advanced the source to e+2 with
    # the target STILL PENDING (this pairing's own promotion never committed), and
    # the session is left in awaiting_acks.
    await a_service._membership(SPACE).update_member_scopes(source_node, ["read"])
    mid = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    tgt = b_config.fingerprint.split(":", 1)[1]
    assert mid.epoch == 3 and next(m for m in mid.members if m.node_id == tgt).status == "pending"

    session = await a_service.store.get_session(pair_id)
    poisoned = session.transition(MeshPairingState.AWAITING_ACKS, now_ms=NOW_MS)
    await a_service.store.put_session(poisoned)

    with pytest.raises(MeshPairingServiceError) as exc:
        await a_service.resume(pair_id)
    assert exc.value.code == "source_unavailable"
    # Failed closed BEFORE re-driving: the source never promoted the target (still
    # PENDING at e+2) and no e+2 activation was delivered — so no split is possible.
    still = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert still.epoch == 3
    assert next(m for m in still.members if m.node_id == tgt).status == "pending"


async def test_assert_no_pairing_activation_filters_role_space_and_state(monkeypatch):
    """The checker resolves source pairing authority from member incarnations.

    A PENDING incarnation is fenced even while its durable session is still
    transferring; ACTIVE incarnations are fenced for awaiting/blocked recovery.
    Other spaces and target-only local state remain unaffected.
    """

    from dataclasses import replace

    from live_mem.core.reservation_guard import PairingActivationError
    from live_mem.mesh.pairing_state import MeshPairingState

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(163, 164)

    # The admitted PENDING member is already shared mutation, so it fences even
    # while the local session is still transferring.
    with pytest.raises(PairingActivationError):
        await a_service.assert_no_pairing_activation(SPACE)
    await a_service.assert_no_pairing_activation("otherspace")

    # Advance the SOURCE session to awaiting_acks -> fences its own space only.
    session = await a_service.store.get_session(pair_id)
    await a_service.store.put_session(
        session.transition(MeshPairingState.AWAITING_ACKS, now_ms=NOW_MS)
    )
    with pytest.raises(PairingActivationError):
        await a_service.assert_no_pairing_activation(SPACE)
    await a_service.assert_no_pairing_activation("otherspace")  # space filter

    # ROLE filter: a TARGET-role session in a mid-activation state does NOT fence
    # (only the promoting SOURCE side can split epochs).
    tgt_session = await b_service.store.get_session(pair_id)
    assert tgt_session.role == "target"
    await b_service.store.put_session(
        replace(tgt_session, state=MeshPairingState.BLOCKED_RECOVERY.value)
    )
    await b_service.assert_no_pairing_activation(SPACE)  # target-role -> no fence


@pytest.mark.parametrize(
    ("terminal_state", "a_seed", "b_seed"),
    [("active", 165, 166), ("cancelled", 167, 168)],
)
async def test_terminal_activation_fence_tail_requires_signed_target_confirmation(
    monkeypatch, terminal_state, a_seed, b_seed
):
    """A mutable terminal session never clears the final source fence alone."""

    from live_mem.mesh.pairing_state import MeshPairingState

    (
        a_service,
        _b_service,
        a_config,
        b_config,
        a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(a_seed, b_seed)
    state_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await state_store.get_membership()
    session = await a_service.store.get_session(pair_id)
    assert membership is not None and session is not None
    target_id = b_config.fingerprint.split(":", 1)[1]

    await a_service.store.put_activation_fence(SPACE, pair_id, now_ms=NOW_MS)
    if terminal_state == "active":
        members = [
            member.model_copy(update={"status": "active"})
            if member.node_id == target_id
            else member
            for member in membership.members
        ]
        terminal = session.transition(
            MeshPairingState.AWAITING_ACKS,
            now_ms=NOW_MS + 1,
            activation_event_id="a" * 32,
        ).transition(MeshPairingState.ACTIVE, now_ms=NOW_MS + 2)
    else:
        members = [
            member for member in membership.members if member.node_id != target_id
        ]
        terminal = session.transition(
            MeshPairingState.BLOCKED_RECOVERY, now_ms=NOW_MS + 1
        ).transition(MeshPairingState.CANCELLED, now_ms=NOW_MS + 2)
    await state_store.set_membership(
        MembershipView(epoch=membership.epoch + 1, members=members)
    )
    await a_service.store.put_session(terminal)

    restarted = MeshPairingService(
        a_config,
        a_storage,
        clock_ms=lambda: NOW_MS + 3,
        sender_factory=lambda _endpoint: None,
    )
    assert await restarted.store.get_activation_fence(SPACE) == pair_id
    if terminal_state == "active":
        # #417's source ACTIVE tail is still mid-protocol without the target's
        # detached readback receipt.  A raw fence phase rewrite/deletion must
        # not turn this synthetic crash prefix into write authority.
        from live_mem.core.reservation_guard import PairingActivationError

        with pytest.raises(PairingActivationError):
            await restarted.assert_no_pairing_activation(SPACE)
        assert await restarted.store.get_activation_fence(SPACE) == pair_id
    else:
        await restarted.assert_no_pairing_activation(SPACE)
        assert await restarted.store.get_activation_fence(SPACE) is None
        # The converged cancellation tail stays idempotently clear.
        await restarted.assert_no_pairing_activation(SPACE)


async def test_activation_migration_sentinel_avoids_terminal_history_ceiling(
    monkeypatch,
):
    """An upgrade with 257+ terminal records seeds clear without a scan."""

    from dataclasses import replace

    (
        a_service,
        b_service,
        _a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        _pair_id,
    ) = await _build_admin_instances(169, 170)
    invitation = await a_service.create_invitation(
        SPACE, requested_scopes=("read", "commit")
    )
    pair_id = invitation["pair_id"]
    template = await a_service.store.get_session(pair_id)
    assert template is not None
    index = 1
    added = 0
    while added < 257:
        historical_id = f"pair_{index:032x}"
        index += 1
        if historical_id == pair_id:
            continue
        await a_service.store.put_session(
            replace(
                template,
                pair_id=historical_id,
                state="cancelled",
                updated_at_ms=NOW_MS + index,
            )
        )
        added += 1
    assert await a_service.store.get_activation_migration(SPACE) is None

    async def history_scan_forbidden(*_args, **_kwargs):
        raise AssertionError("migrated activation authority must be targeted")

    a_service.store.list_sessions = history_scan_forbidden  # type: ignore[method-assign]
    await b_service.accept_invitation(
        invitation["invitation_bytes"],
        SPACE,
        secret=invitation["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read", "commit"), quiesced=True,
    )
    approved = await a_service.approve(pair_id)
    assert approved["state"] == "transferring"
    # The legacy inventory remains clear without a history scan, then the new
    # source flow deliberately re-arms the same targeted per-space owner before
    # Transition 1 so marker/fence/incarnation loss cannot hide this tail.
    assert await a_service.store.get_activation_migration(SPACE) == pair_id
    membership = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_membership()  # type: ignore[arg-type]
    assert membership is not None
    assert len([m for m in membership.members if m.status == "pending"]) == 1


async def test_pre_mesh_multi_active_roster_ignores_terminal_history(monkeypatch):
    """Incarnation-less legacy members cannot encode a hidden P10 activation."""

    from dataclasses import replace

    (
        a_service,
        _b_service,
        _a_config,
        b_config,
        a_storage,
        _b_storage,
        _peers,
        _pair_id,
    ) = await _build_admin_instances(171, 172)
    invitation = await a_service.create_invitation(
        SPACE, requested_scopes=("read", "commit")
    )
    pair_id = invitation["pair_id"]
    template = await a_service.store.get_session(pair_id)
    assert template is not None
    index = 1
    added = 0
    while added < 256:
        historical_id = f"pair_{index:032x}"
        index += 1
        if historical_id == pair_id:
            continue
        await a_service.store.put_session(
            replace(
                template,
                pair_id=historical_id,
                state="cancelled",
                updated_at_ms=NOW_MS + index,
            )
        )
        added += 1
    assert await a_service.store.get_activation_migration(SPACE) is None
    store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await store.get_membership()
    assert membership is not None
    await store.set_membership(
        MembershipView(
            epoch=membership.epoch,
            members=[
                *membership.members,
                Member(
                    node_id="legacyactive000000000000000000000",
                    public_key=_legacy(b_config.public_key),
                ),
            ],
        )
    )

    async def history_scan_forbidden(*_args, **_kwargs):
        raise AssertionError("append-only history is not activation authority")

    a_service.store.list_sessions = history_scan_forbidden  # type: ignore[method-assign]
    await a_service.assert_no_pairing_activation(SPACE)
    assert await a_service.store.get_activation_migration(SPACE) == ""


async def test_legacy_mutating_session_is_indexed_and_never_omitted(monkeypatch):
    from dataclasses import replace

    from live_mem.core.reservation_guard import PairingActivationError

    (
        a_service,
        _b_service,
        _a_config,
        b_config,
        a_storage,
        _b_storage,
        _peers,
        _pair_id,
    ) = await _build_admin_instances(173, 174)
    invitation = await a_service.create_invitation(
        SPACE, requested_scopes=("read",)
    )
    pair_id = invitation["pair_id"]
    session = await a_service.store.get_session(pair_id)
    assert session is not None
    await a_service.store.put_session(
        replace(session, state="awaiting_acks", updated_at_ms=NOW_MS + 1)
    )
    state_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await state_store.get_membership()
    assert membership is not None
    await state_store.set_membership(
        MembershipView(
            epoch=membership.epoch + 1,
            members=[
                *membership.members,
                Member(
                    node_id=b_config.fingerprint.split(":", 1)[1],
                    public_key=_legacy(b_config.public_key),
                    incarnation=pair_id,
                ),
            ],
        )
    )

    with pytest.raises(PairingActivationError):
        await a_service.assert_no_pairing_activation(SPACE)
    assert await a_service.store.get_activation_migration(SPACE) == pair_id

    await a_service.store.put_session(
        replace(session, state="active", updated_at_ms=NOW_MS + 2)
    )
    await a_service.assert_no_pairing_activation(SPACE)
    assert await a_service.store.get_activation_migration(SPACE) == ""


async def test_legacy_approved_pending_owner_remains_evictable() -> None:
    """Crash-after-admit APPROVED evidence is a valid PENDING owner."""

    from dataclasses import replace

    (
        a_service,
        _b_service,
        _a_config,
        b_config,
        a_storage,
        _b_storage,
        _peers,
        _pair_id,
    ) = await _build_admin_instances(179, 180)
    invitation = await a_service.create_invitation(
        SPACE, requested_scopes=("read",)
    )
    pair_id = invitation["pair_id"]
    session = await a_service.store.get_session(pair_id)
    assert session is not None
    await a_service.store.put_session(
        replace(session, state="approved", updated_at_ms=NOW_MS + 1)
    )
    state_store = HivemindStateStore(storage=a_storage, space_id=SPACE)  # type: ignore[arg-type]
    membership = await state_store.get_membership()
    assert membership is not None
    await state_store.set_membership(
        MembershipView(
            epoch=membership.epoch + 1,
            members=[
                *membership.members,
                Member(
                    node_id=b_config.fingerprint.split(":", 1)[1],
                    public_key=_legacy(b_config.public_key),
                    status="pending",
                    incarnation=pair_id,
                ),
            ],
        )
    )

    await a_service.assert_no_pairing_activation(
        SPACE, ignore_pair_id=pair_id
    )
    assert await a_service.store.get_activation_migration(SPACE) == pair_id
    # Crash after persisting the owner sentinel but before removing PENDING:
    # the exact retry must remain authorized and evictable.
    await a_service.assert_no_pairing_activation(
        SPACE, ignore_pair_id=pair_id
    )


async def test_legacy_activation_event_resolves_target_reservation_not_history():
    """A payload without pair_id still works above the historical session cap."""

    from dataclasses import replace

    (
        a_service,
        b_service,
        a_config,
        _b_config,
        _a_storage,
        _b_storage,
        _peers,
        _pair_id,
    ) = await _build_admin_instances(175, 176)
    invitation = await a_service.create_invitation(
        SPACE, requested_scopes=("read", "commit")
    )
    pair_id = invitation["pair_id"]
    await b_service.accept_invitation(
        invitation["invitation_bytes"],
        SPACE,
        secret=invitation["secret"],
        source_endpoint=A_URL,
        requested_scopes=("read", "commit"), quiesced=True,
    )
    target_session = await b_service.store.get_session(pair_id)
    assert target_session is not None and target_session.state == "claimed"
    target_session = replace(
        target_session, state="transferring", updated_at_ms=NOW_MS + 1
    )
    await b_service.store.put_session(target_session)
    index = 1
    added = 0
    while added < 256:
        historical_id = f"pair_{index:032x}"
        index += 1
        if historical_id == pair_id:
            continue
        await b_service.store.put_session(
            replace(
                target_session,
                pair_id=historical_id,
                state="cancelled",
                updated_at_ms=NOW_MS + index,
            )
        )
        added += 1

    async def history_scan_forbidden(*_args, **_kwargs):
        raise AssertionError("legacy activation resolution must be targeted")

    b_service.store.list_sessions = history_scan_forbidden  # type: ignore[method-assign]
    resolved = await b_service._find_target_session(
        SPACE, a_config.fingerprint, pair_id=""
    )
    assert resolved is not None and resolved.pair_id == pair_id


async def test_finalized_legacy_activation_reconfirms_without_session_scan():
    """Post-release HEALTHY authority can confirm a pre-pair_id activation."""

    from dataclasses import replace
    from types import SimpleNamespace

    from live_mem.mesh.membership_sync import candidate_view_digest

    (
        a_service,
        b_service,
        a_config,
        _b_config,
        a_storage,
        _b_storage,
        _peers,
        pair_id,
    ) = await _drive_to_approved(177, 178)
    activated = await b_service.run_target_enrollment(pair_id)
    assert activated["state"] == "active"
    assert await b_service.store.get_reservation(SPACE) is None
    source_session = await a_service.store.get_session(pair_id)
    target_session = await b_service.store.get_session(pair_id)
    assert source_session is not None and target_session is not None
    event = await HivemindStateStore(
        storage=a_storage, space_id=SPACE
    ).get_event(source_session.activation_event_id)  # type: ignore[arg-type]
    assert event is not None and "pair_id" not in event.payload
    target_membership = await HivemindStateStore(
        storage=_b_storage, space_id=SPACE
    ).get_membership()  # type: ignore[arg-type]
    assert target_membership is not None
    legacy_event = event.model_copy(
        update={
            "payload": {
                **event.payload,
                "candidate_view_digest": candidate_view_digest(
                    target_membership
                ),
            }
        }
    )

    index = 1
    added = 0
    while added < 256:
        historical_id = f"pair_{index:032x}"
        index += 1
        if historical_id == pair_id:
            continue
        await b_service.store.put_session(
            replace(
                target_session,
                pair_id=historical_id,
                state="cancelled",
                updated_at_ms=NOW_MS + index,
            )
        )
        added += 1

    confirmation = await b_service.try_activation_reconfirmation(
        SimpleNamespace(
            space_id=SPACE,
            source_fingerprint=a_config.fingerprint,
            source_public_key=a_config.public_key,
            nonce="nonce_" + "a" * 32,
        ),
        legacy_event,
    )
    assert confirmation is not None
    assert json.loads(confirmation.body) == {
        "epoch": legacy_event.membership_epoch,
        "state": "active",
    }
