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
    NodeIdentity,
)
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
    decode_mesh_public_key,
    generate_mesh_identity,
    parse_mesh_private_key,
)
from live_mem.mesh.pairing_client import PeerResponse
from live_mem.mesh.pairing_service import MeshPairingService
from live_mem.mesh.router import MeshNamespaceRouter
from tests.test_hivemind_state import FakeStorage
from tests.test_mesh_router import FakeProcessLock, FakeReplayLedger, _invoke

NOW_MS = 1_800_000_000_000
SPACE = "meshspace"
A_URL = "https://a.mesh.test"
B_URL = "https://b.mesh.test"


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


async def test_two_tcp_asgi_admins_pair_without_in_process_peer_transport(caplog: pytest.LogCaptureFixture) -> None:
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
    await _seed_source(a_storage, a_config)
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
            },
        )
        assert status == 200 and accepted["state"] == "claimed"
        status, approved = await _admin_tcp_post(a_url, "approve", {"pair_id": invitation["pair_id"]})
        assert status == 200 and approved["epoch"] == 2
        status, enrolled = await _admin_tcp_post(b_url, "enroll", {"pair_id": invitation["pair_id"]})
        assert status == 200 and enrolled["state"] == "active"

    # The underlying stores agree at e+2, and each direction crossed at least
    # one actual signed peer route.  A direct ASGI call cannot satisfy either
    # transcript assertion because _LoopbackHttpSender owns the only sender.
    a_members = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    b_members = await HivemindStateStore(storage=b_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    target_id = b_config.fingerprint.split(":", 1)[1]
    assert a_members.epoch == b_members.epoch == 3
    assert any(member.node_id == target_id and member.status == "active" for member in a_members.members)
    assert any(member.node_id == target_id and member.status == "active" for member in b_members.members)
    assert any(path == "/mesh/v1/pair/claim" for _method, path in b_transcript)
    assert any(path == "/mesh/v1/events" for _method, path in a_transcript)
    persisted = "\n".join([*a_storage.objects.values(), *b_storage.objects.values()])
    assert secret not in persisted and a_private not in persisted and b_private not in persisted
    assert snapshot_canary not in persisted

    audit_entries = [
        json.loads(record.getMessage())
        for record in caplog.records
        if record.name == "live_mem.audit"
    ]
    assert any(entry["path"] == "/api/admin/mesh/invitation" for entry in audit_entries)
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
        requested_scopes=("read", "propose", "commit"),
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
    register_reservation_checker(b_service.store.assert_space_not_reserved)
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
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))
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
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))
    await a_service.approve(pair_id)
    return a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id


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
        {"invitation": inv["invitation"], "target_space_id": SPACE, "secret": inv["secret"], "source_endpoint": A_URL, "scopes": ["read", "commit"]},
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
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))

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
    await b_service.accept_invitation(invite["invitation_bytes"], SPACE, secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))

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


async def test_accept_refuses_space_mismatch(monkeypatch):
    # The reserved space MUST equal the enrolled space; a mismatch is refused
    # before any reservation is taken.
    from live_mem.mesh.pairing_service import MeshPairingServiceError

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, _ = await _build_admin_instances(109, 110)
    invite = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    with pytest.raises(MeshPairingServiceError) as e:
        await b_service.accept_invitation(
            invite["invitation_bytes"], "othermeshspace",
            secret=invite["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"),
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
    await b_service.accept_invitation(inv1["invitation_bytes"], SPACE, secret=inv1["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))
    await c_service.accept_invitation(inv2["invitation_bytes"], SPACE, secret=inv2["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))

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
    await b_service.accept_invitation(inv1["invitation_bytes"], SPACE, secret=inv1["secret"], source_endpoint=A_URL, requested_scopes=("read",))
    await a_service.approve(inv1["pair_id"])  # pairing 1 admits its target PENDING

    # A SECOND invitation minted AFTER the first admitted has a fresh base_epoch, so
    # it passes the epoch check — the PENDING-candidate gate must still refuse it.
    inv2 = await a_service.create_invitation(SPACE, requested_scopes=("read",))
    await c_service.accept_invitation(inv2["invitation_bytes"], SPACE, secret=inv2["secret"], source_endpoint=A_URL, requested_scopes=("read",))
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
    await b_service.accept_invitation(inv["invitation_bytes"], SPACE, secret=inv["secret"], source_endpoint=A_URL, requested_scopes=("read", "commit"))

    # Park approve at put_session('approved') — after its early epoch read, BEFORE it
    # acquires the membership lock — so the rescope can advance the epoch.
    gate = asyncio.Event()
    park_reached = asyncio.Event()
    real_put = a_service.store.put_session

    async def gated_put(session):
        if session.role == "source" and session.state == "approved" and not park_reached.is_set():
            park_reached.set()
            await gate.wait()
        return await real_put(session)

    a_service.store.put_session = gated_put  # type: ignore[method-assign]
    task = asyncio.ensure_future(a_service.approve(pair_id))
    await park_reached.wait()
    # Concurrent rescope advances the source epoch base(1) -> 2.
    await a_service._membership(SPACE).update_member_scopes("sourcenode0000000000000000000000", ["read"])
    gate.set()
    with pytest.raises(MeshPairingServiceError) as e:
        await task
    assert e.value.code == "epoch_changed"
    a_service.store.put_session = real_put  # type: ignore[method-assign]

    # No admission happened at the wrong epoch: only the rescope's bump.
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 2
    assert not any(m.status == "pending" for m in a_mem.members)


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

    # B re-enrolls with the SAME identity: reset its space to blank, then pair anew.
    await b_service._teardown_target_space(SPACE)
    invite2 = await a_service.create_invitation(SPACE, requested_scopes=("read", "commit"))
    pair_id2 = invite2["pair_id"]
    await b_service.accept_invitation(
        invite2["invitation_bytes"], SPACE, secret=invite2["secret"], source_endpoint=A_URL,
        requested_scopes=("read", "commit"),
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

    monkeypatch.setattr(a_service, "_deliver_activation", boom)
    result = await b_service.run_target_enrollment(pair_id)
    assert result["state"] != "active"
    a_sess = await a_service.store.get_session(pair_id)
    assert a_sess.state == "awaiting_acks"  # crash window: promoted but not persisted active
    tgt = b_config.fingerprint.split(":", 1)[1]
    a_mem = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert a_mem.epoch == 3 and any(m.node_id == tgt and m.status == "active" for m in a_mem.members)

    monkeypatch.undo()  # restore _deliver_activation

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
        a_service._sender_factory = lambda _e: _RescopeAtActivation(peers, "B")  # type: ignore[attr-defined]
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
        a_service._sender_factory = lambda _e: _AdmitAtActivation(peers, "B")  # type: ignore[attr-defined]
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
        a_service._sender_factory = lambda _e: _EvictAtActivation(peers, "B")  # type: ignore[attr-defined]
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
    assert exc.value.code == "unrecoverable_epoch"
    # Failed closed BEFORE re-driving: the source never promoted the target (still
    # PENDING at e+2) and no e+2 activation was delivered — so no split is possible.
    still = await HivemindStateStore(storage=a_storage, space_id=SPACE).get_membership()  # type: ignore[arg-type]
    assert still.epoch == 3
    assert next(m for m in still.members if m.node_id == tgt).status == "pending"


async def test_assert_no_pairing_activation_filters_role_space_and_state(monkeypatch):
    """The real checker fences ONLY a SOURCE session for the SAME space in a
    mid-activation state (awaiting_acks / blocked_recovery) — not other states,
    not other spaces, not TARGET-role sessions."""

    from dataclasses import replace

    from live_mem.core.reservation_guard import PairingActivationError
    from live_mem.mesh.pairing_state import MeshPairingState

    a_service, b_service, a_config, b_config, a_storage, b_storage, peers, pair_id = await _drive_to_approved(163, 164)

    # SOURCE session is 'transferring' (not mid-activation) -> no fence, any space.
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
