# -*- coding: utf-8 -*-
"""Admin control plane /api/admin/mesh/* tests (P10-3, issue #191)."""

from __future__ import annotations

import base64
import json

import pytest

from live_mem.auth.context import current_token_info
from live_mem.mesh.mesh_admin import MeshAdminMiddleware
from live_mem.mesh.pairing_service import MeshPairingService
from live_mem.mesh.pairing_state import (
    BlockedRecoveryEvidence,
    MeshPairingState,
    SignedBlockedRecoveryEvidence,
)
from tests.test_hivemind_state import FakeStorage
from tests.test_mesh_pairing_e2e import NOW_MS, SPACE, _config, _seed_source

A_PRIV = "ed25519-private:v1:" + base64.urlsafe_b64encode(bytes([61]) * 32).decode().rstrip("=")


async def _fallback(scope, receive, send):
    await send({"type": "http.response.start", "status": 299, "headers": []})
    await send({"type": "http.response.body", "body": b"passthrough"})


async def _invoke(app, method, path, *, body=b"", admin=True, origin=b"https://a.mesh.test", host=b"a.mesh.test"):
    headers = [(b"host", host)]
    if origin is not None:
        headers.append((b"origin", origin))
    scope = {"type": "http", "method": method, "path": path, "headers": headers, "state": {}, "_body": body}
    messages: list = []

    async def receive():
        return {"type": "http.request", "body": scope.pop("_body", b""), "more_body": False}

    async def send(m):
        messages.append(m)

    token = None
    if admin:
        token = current_token_info.set({"permissions": ["admin", "read", "write"], "client_name": "op"})
    try:
        await app(scope, receive, send)
    finally:
        if token is not None:
            current_token_info.reset(token)
    status = messages[0]["status"]
    payload = json.loads(messages[1]["body"]) if len(messages) > 1 and messages[1].get("body") else {}
    return status, payload


async def _service():
    storage = FakeStorage()
    config = _config(A_PRIV, "https://a.mesh.test")
    await _seed_source(storage, config)
    svc = MeshPairingService(config, storage, clock_ms=lambda: NOW_MS, sender_factory=lambda _e: None)
    return MeshAdminMiddleware(_fallback, svc), svc


async def test_non_admin_is_refused():
    app, _svc = await _service()
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status", admin=False)
    assert status == 403


async def test_refuses_when_process_lock_not_acquired():
    # A worker that lost/never held the Mesh leader lock cannot serve mutations
    # (mirrors the peer router's per-request process-identity recheck).
    storage = FakeStorage()
    config = _config(A_PRIV, "https://a.mesh.test")
    await _seed_source(storage, config)
    svc = MeshPairingService(config, storage, clock_ms=lambda: NOW_MS, sender_factory=lambda _e: None)

    class _Lock:
        acquired = False

    app = MeshAdminMiddleware(_fallback, svc, process_lock=_Lock())
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 503


async def test_non_mesh_path_passes_through():
    app, _svc = await _service()
    scope = {"type": "http", "method": "GET", "path": "/admin", "headers": [], "state": {}}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    seen = []

    async def send(m):
        seen.append(m)

    await app(scope, receive, send)
    assert seen[0]["status"] == 299  # fell through to the wrapped app


async def test_status_lists_pairings():
    app, svc = await _service()
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 200 and payload["enabled"] is True and payload["pairings"] == []


async def test_create_invitation_requires_confirmation():
    app, svc = await _service()
    status, payload = await _invoke(
        app, "POST", "/api/admin/mesh/invitation",
        body=json.dumps({"space_id": SPACE, "scopes": ["read"]}).encode(),
    )
    assert status == 400 and "confirmation" in payload["message"]


async def test_create_invitation_returns_secret_once():
    app, svc = await _service()
    status, payload = await _invoke(
        app, "POST", "/api/admin/mesh/invitation",
        body=json.dumps({"confirm": True, "space_id": SPACE, "scopes": ["read", "commit"]}).encode(),
    )
    assert status == 200
    assert payload["pair_id"].startswith("pair_")
    assert payload["secret"]  # shown once
    # The durable session stores only a digest, never the raw secret.
    session = await svc.store.get_session(payload["pair_id"])
    assert session is not None and session.secret_digest and payload["secret"] not in session.secret_digest


async def test_cross_origin_mutation_is_refused():
    app, svc = await _service()
    status, payload = await _invoke(
        app, "POST", "/api/admin/mesh/invitation",
        body=json.dumps({"confirm": True, "space_id": SPACE}).encode(),
        origin=b"https://evil.example",
    )
    assert status == 403 and "cross-origin" in payload["message"]


async def test_approve_unknown_pair_is_safe_error():
    app, svc = await _service()
    status, payload = await _invoke(
        app, "POST", "/api/admin/mesh/approve",
        body=json.dumps({"confirm": True, "pair_id": "pair_" + "a" * 32}).encode(),
    )
    assert status == 400 and payload["code"] == "unknown_pair"


async def test_status_includes_widened_instance_fields():
    app, svc = await _service()
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 200
    assert payload["healthy"] is True
    assert payload["display_name"] == "peer"
    assert payload["public_url"] == "https://a.mesh.test"
    assert payload["fingerprint"] == svc._config.fingerprint


async def test_status_widens_pairing_projection():
    app, svc = await _service()
    _, created = await _invoke(
        app, "POST", "/api/admin/mesh/invitation",
        body=json.dumps({"confirm": True, "space_id": SPACE, "scopes": ["read"]}).encode(),
    )
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 200
    entry = next(p for p in payload["pairings"] if p["pair_id"] == created["pair_id"])
    assert entry["source_fingerprint"] == svc._config.fingerprint
    assert entry["source_endpoint"] == "https://a.mesh.test"
    assert entry["target_fingerprint"] == ""
    assert entry["granted_scopes"] == ["read"]
    assert isinstance(entry["created_at_ms"], int)
    assert isinstance(entry["updated_at_ms"], int)
    assert isinstance(entry["expires_at_ms"], int)
    assert entry["last_error"] == ""
    assert "next_action" not in entry  # only blocked_recovery sessions carry it


async def test_status_surfaces_blocked_recovery_next_action():
    app, svc = await _service()
    _, created = await _invoke(
        app, "POST", "/api/admin/mesh/invitation",
        body=json.dumps({"confirm": True, "space_id": SPACE}).encode(),
    )
    pair_id = created["pair_id"]
    session = await svc.store.get_session(pair_id)
    session = session.transition(MeshPairingState.CLAIMED, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.APPROVED, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.TRANSFERRING, now_ms=NOW_MS)
    session = session.transition(
        MeshPairingState.BLOCKED_RECOVERY, now_ms=NOW_MS, last_error="bootstrap_export_failed"
    )
    await svc.store.put_session(session)
    evidence = BlockedRecoveryEvidence(
        pair_id=pair_id,
        space_id=SPACE,
        epoch=1,
        phase="bootstrap_export_failed",
        next_action="evict",
        manifest_digest="",
        candidate_view_digest="",
        activation_event_id="",
        issued_at_ms=NOW_MS,
    )
    signed = SignedBlockedRecoveryEvidence.sign(evidence, svc._config.private_key)
    await svc.store.put_evidence(pair_id, signed)

    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    entry = next(p for p in payload["pairings"] if p["pair_id"] == pair_id)
    assert entry["next_action"] == "evict"
    assert entry["phase"] == "bootstrap_export_failed"


async def test_status_omits_next_action_when_evidence_unverifiable():
    # A blocked session with NO stored evidence (e.g. a corrupted/partial
    # write) must never crash the status read — it just omits the hint.
    app, svc = await _service()
    _, created = await _invoke(
        app, "POST", "/api/admin/mesh/invitation",
        body=json.dumps({"confirm": True, "space_id": SPACE}).encode(),
    )
    pair_id = created["pair_id"]
    session = await svc.store.get_session(pair_id)
    session = session.transition(MeshPairingState.CLAIMED, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.APPROVED, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.TRANSFERRING, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.BLOCKED_RECOVERY, now_ms=NOW_MS)
    await svc.store.put_session(session)

    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 200
    entry = next(p for p in payload["pairings"] if p["pair_id"] == pair_id)
    assert "next_action" not in entry


async def test_members_rejects_invalid_space_id():
    app, _svc = await _service()
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/members/../etc")
    assert status == 400 and "invalid space id" in payload["message"]


async def test_members_non_admin_is_refused():
    app, _svc = await _service()
    status, payload = await _invoke(app, "GET", f"/api/admin/mesh/members/{SPACE}", admin=False)
    assert status == 403


async def test_members_lists_active_members():
    app, svc = await _service()
    status, payload = await _invoke(app, "GET", f"/api/admin/mesh/members/{SPACE}")
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["space_id"] == SPACE
    assert payload["membership_epoch"] == 1
    assert len(payload["members"]) == 1
    member = payload["members"][0]
    assert member["node_id"] == "sourcenode0000000000000000000000"
    # The pre-existing local Hivemind node identity predates Mesh and has no
    # fingerprint relationship to it; nothing to enrich with, so it stays
    # unset rather than fabricated.
    assert member["fingerprint"] == ""
    assert member["display_name"] == ""


async def test_members_enriches_admitted_peer_from_pairing_session_fingerprint():
    # A Mesh-admitted member's node_id IS the hex half of its fingerprint (by
    # construction in approve()'s admit_pending_candidate_locked call) — this
    # is the case the enrichment join is actually for. Seed that peer member
    # plus a matching pairing session directly (bypassing the full approve()
    # flow, which needs a second live instance) to prove the join.
    from live_mem.core.hivemind import HivemindStateStore, Member, MembershipView
    from live_mem.mesh.identity import generate_mesh_identity
    from live_mem.mesh.pairing_state import MeshPairingState

    app, svc = await _service()
    peer = generate_mesh_identity()
    peer_node_id = peer.fingerprint.split(":", 1)[1]

    # create_invitation() requires exactly one active member, so mint the
    # invitation FIRST — the fabricated peer joins afterward, matching the
    # real post-pairing end state (both self and admitted peer end up
    # active).
    _, created = await _invoke(
        app, "POST", "/api/admin/mesh/invitation",
        body=json.dumps({"confirm": True, "space_id": SPACE}).encode(),
    )

    store = HivemindStateStore(storage=svc._storage_factory(), space_id=SPACE)
    membership = await store.get_membership()
    await store.set_membership(
        MembershipView(
            epoch=membership.epoch,
            members=[
                *membership.members,
                Member(node_id=peer_node_id, public_key=peer.public_key, scopes=["read"]),
            ],
        )
    )

    session = await svc.store.get_session(created["pair_id"])
    session = session.with_fields(
        now_ms=NOW_MS,
        target_public_key=peer.public_key,
        target_fingerprint=peer.fingerprint,
        target_endpoint="https://b.mesh.test",
    )
    session = session.transition(MeshPairingState.CLAIMED, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.APPROVED, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.TRANSFERRING, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.AWAITING_ACKS, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.ACTIVE, now_ms=NOW_MS)
    await svc.store.put_session(session)

    status, payload = await _invoke(app, "GET", f"/api/admin/mesh/members/{SPACE}")
    assert status == 200
    member = next(m for m in payload["members"] if m["node_id"] == peer_node_id)
    assert member["fingerprint"] == peer.fingerprint
    assert member["endpoint"] == "https://b.mesh.test"
    assert member["scopes"] == ["read"]
