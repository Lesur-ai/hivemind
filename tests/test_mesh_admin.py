# -*- coding: utf-8 -*-
"""Admin control plane /api/admin/mesh/* tests (P10-3, issue #191)."""

from __future__ import annotations

import base64
import json

import pytest

from live_mem.auth.context import current_token_info
from live_mem.core.hivemind import layout
from live_mem.core.hivemind.state import HivemindStateStore
from live_mem.core.space import SpaceService
from live_mem.mesh.mesh_admin import MeshAdminMiddleware
from live_mem.mesh.pairing_service import (
    MeshPairingService,
    MeshPairingServiceError,
)
from live_mem.mesh.pairing_store import MeshPairingStoreError
from live_mem.mesh.pairing_state import (
    BlockedRecoveryEvidence,
    MeshPairingState,
    SignedBlockedRecoveryEvidence,
)
from tests.test_hivemind_state import FakeStorage
from tests.test_mesh_pairing_e2e import (
    NOW_MS,
    SPACE,
    _config,
    _seed_blank_target,
    _seed_source,
)

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


async def _local_service():
    """A real committed local-only space, suitable for the #413 transition."""

    storage = FakeStorage()
    config = _config(A_PRIV, "https://a.mesh.test")
    await _seed_blank_target(storage)
    svc = MeshPairingService(
        config,
        storage,
        clock_ms=lambda: NOW_MS,
        sender_factory=lambda _e: None,
    )
    return MeshAdminMiddleware(_fallback, svc), svc, storage


async def _clone_source_space(storage: FakeStorage, target_space_id: str) -> None:
    """Clone the real legacy source fixture under one neighbouring space id."""

    for key, value in list(storage.snapshot().items()):
        if not key.startswith(f"{SPACE}/"):
            continue
        relative = key[len(SPACE) + 1 :]
        if relative == "_meta.json":
            meta = json.loads(value)
            meta["space_id"] = target_space_id
            value = json.dumps(meta)
        await storage.put(f"{target_space_id}/{relative}", value)


async def test_non_admin_is_refused():
    app, _svc = await _service()
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status", admin=False)
    assert status == 403
    status, payload = await _invoke(
        app, "GET", "/api/admin/mesh/availability", admin=False
    )
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
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/availability")
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


async def test_availability_is_exact_and_does_not_scan_mesh_state() -> None:
    app, svc = await _service()

    async def _unexpected_scan(*_args, **_kwargs):
        raise AssertionError("availability must not scan sessions or source readiness")

    svc.store.list_sessions = _unexpected_scan
    svc.list_source_eligibility = _unexpected_scan
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/availability")
    assert status == 200
    assert payload == {"status": "ok"}


async def test_status_projects_source_readiness_and_eligible_spaces() -> None:
    app, _svc = await _service()
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 200
    source = next(item for item in payload["source_readiness"] if item["space_id"] == SPACE)
    assert source["state"] == "ready"
    assert source["can_create_invitation"] is True
    assert payload["eligible_spaces"] == [SPACE]
    assert payload["eligible_spaces"] == [
        item["space_id"]
        for item in payload["source_readiness"]
        if item["can_create_invitation"] is True
    ]
    assert payload["pairings_truncated"] is False
    assert payload["source_readiness_unavailable"] is False
    assert payload["source_readiness_truncated"] is False
    assert payload["source_readiness_unavailable_reason"] == ""


async def test_status_scans_pairing_history_once_and_readiness_does_not() -> None:
    app, svc = await _service()
    calls = 0
    seen_limits = []
    original = svc.store.list_sessions_diagnostic

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        seen_limits.append(kwargs.get("max_sessions"))
        return await original(*args, **kwargs)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("status readiness must not scan authoritative history")

    svc.store.list_sessions_diagnostic = counted
    svc.store.list_sessions = forbidden
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 200
    assert payload["pairings_truncated"] is False
    assert calls == 1
    assert seen_limits == [48]


async def test_status_reports_diagnostic_history_truncation() -> None:
    app, svc = await _service()

    async def truncated(*_args, **_kwargs):
        return [], True

    svc.store.list_sessions_diagnostic = truncated
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 200
    assert payload["pairings"] == []
    assert payload["pairings_truncated"] is True


@pytest.mark.parametrize(
    "code",
    ["mesh_status_inventory_unavailable", "mesh_status_inventory_too_large"],
)
async def test_status_inventory_failures_preserve_recovery_surfaces(code) -> None:
    app, svc = await _service()

    _, invitation = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/invitation",
        body=json.dumps(
            {"confirm": True, "space_id": SPACE, "scopes": ["read"]}
        ).encode(),
    )

    async def unavailable():
        raise MeshPairingServiceError(code, "source inventory is unavailable")

    svc.list_source_eligibility = unavailable
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")
    assert status == 200
    assert payload["status"] == "ok"
    assert [item["pair_id"] for item in payload["pairings"]] == [
        invitation["pair_id"]
    ]
    assert payload["source_readiness"] == []
    assert payload["eligible_spaces"] == []
    assert payload["source_readiness_unavailable"] is True
    assert payload["source_readiness_truncated"] is (
        code == "mesh_status_inventory_too_large"
    )
    assert payload["source_readiness_unavailable_reason"] == code


async def test_status_over_128_spaces_preserves_pairings_and_marks_truncation() -> None:
    app, svc = await _service()
    _, invitation = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/invitation",
        body=json.dumps(
            {"confirm": True, "space_id": SPACE, "scopes": ["read"]}
        ).encode(),
    )
    storage = svc._storage_factory()
    for index in range(128):
        await storage.put(f"overflow-{index:03d}/_meta.json", "{}")

    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")

    assert status == 200
    assert [item["pair_id"] for item in payload["pairings"]] == [
        invitation["pair_id"]
    ]
    assert payload["source_readiness"] == []
    assert payload["eligible_spaces"] == []
    assert payload["source_readiness_unavailable"] is True
    assert payload["source_readiness_truncated"] is True
    assert (
        payload["source_readiness_unavailable_reason"]
        == "mesh_status_inventory_too_large"
    )


async def test_targeted_source_readiness_reuses_service_predicate() -> None:
    app, _svc = await _service()
    status, payload = await _invoke(
        app,
        "GET",
        f"/api/admin/mesh/source-readiness/{SPACE}",
    )
    assert status == 200
    assert payload["status"] == "ok"
    assert payload["source"]["space_id"] == SPACE
    assert payload["source"]["state"] == "ready"


async def test_targeted_source_readiness_inventory_error_stays_fail_closed() -> None:
    app, svc = await _service()

    async def unavailable(_space_id: str):
        raise MeshPairingServiceError(
            "mesh_status_inventory_unavailable",
            "source inventory is unavailable",
        )

    svc.inspect_source_eligibility = unavailable
    status, payload = await _invoke(
        app,
        "GET",
        f"/api/admin/mesh/source-readiness/{SPACE}",
    )
    assert status == 503
    assert payload == {
        "status": "error",
        "code": "mesh_status_inventory_unavailable",
        "message": "source inventory is unavailable",
    }


async def test_corrupt_selected_commit_targeted_readiness_is_stable_unsafe() -> None:
    app, svc = await _service()
    storage = svc._storage_factory()
    marker = "CORRUPT_SELECTED_COMMIT_MUST_NOT_LEAK"
    storage.objects[layout.commit_key(SPACE, 1)] = marker + "{"  # invalid JSON

    first_status, first = await _invoke(
        app,
        "GET",
        f"/api/admin/mesh/source-readiness/{SPACE}",
    )
    second_status, second = await _invoke(
        app,
        "GET",
        f"/api/admin/mesh/source-readiness/{SPACE}",
    )

    assert first_status == second_status == 200
    for payload in (first, second):
        source = payload["source"]
        assert payload["status"] == "ok"
        assert source["state"] == "unsafe"
        assert source["source_ready"] is False
        assert source["source_initializable"] is False
        assert source["can_create_invitation"] is False
        assert source["resumable"] is False
        assert len(source["state_token"]) == 64
        assert marker not in json.dumps(payload)
    assert first["source"]["state_token"] == second["source"]["state_token"]


async def test_non_utf8_selected_commit_is_confined_to_unsafe_source() -> None:
    app, svc = await _service()
    storage = svc._storage_factory()
    selected_key = layout.commit_key(SPACE, 1)
    original_get = storage.get

    async def invalid_utf8_get(key: str):
        if key == selected_key:
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return await original_get(key)

    storage.get = invalid_utf8_get
    targeted_status, targeted = await _invoke(
        app,
        "GET",
        f"/api/admin/mesh/source-readiness/{SPACE}",
    )
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")

    assert targeted_status == status == 200
    assert targeted["source"]["state"] == "unsafe"
    assert payload["source_readiness"][0]["state"] == "unsafe"
    assert payload["eligible_spaces"] == []


@pytest.mark.parametrize("fault", ["list", "get"])
async def test_selected_commit_backend_failure_is_local_unavailable_not_unsafe(
    fault: str,
) -> None:
    app, svc = await _service()
    storage = svc._storage_factory()
    neighbour = "healthy-neighbour"
    await _clone_source_space(storage, neighbour)
    _, invitation = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/invitation",
        body=json.dumps(
            {"confirm": True, "space_id": SPACE, "scopes": ["read"]}
        ).encode(),
    )
    selected_key = layout.commit_key(SPACE, 1)
    if fault == "list":
        original_list = storage.list_objects

        async def unavailable_selected_commit(prefix: str, max_keys: int = 0):
            if prefix == selected_key:
                raise OSError("storage temporarily unavailable")
            return await original_list(prefix, max_keys=max_keys)

        storage.list_objects = unavailable_selected_commit
    else:
        original_get = storage.get

        async def unavailable_selected_commit(key: str):
            if key == selected_key:
                raise OSError("storage temporarily unavailable")
            return await original_get(key)

        storage.get = unavailable_selected_commit
    targeted_status, targeted = await _invoke(
        app,
        "GET",
        f"/api/admin/mesh/source-readiness/{SPACE}",
    )
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")

    assert targeted_status == status == 200
    source = targeted["source"]
    assert source["state"] == source["hive_status"] == "unavailable"
    assert source["source_ready"] is False
    assert source["source_initializable"] is False
    assert source["can_create_invitation"] is False
    assert source["resumable"] is False
    sources = {item["space_id"]: item for item in payload["source_readiness"]}
    assert sources[SPACE]["state"] == "unavailable"
    assert sources[neighbour]["state"] == "ready"
    assert payload["eligible_spaces"] == [neighbour]
    assert [item["pair_id"] for item in payload["pairings"]] == [
        invitation["pair_id"]
    ]
    assert payload["source_readiness_unavailable"] is False
    assert "storage temporarily unavailable" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("head", "key"),
    [
        ("product", f"{SPACE}/_meta.json"),
        ("critical", layout.members_key(SPACE)),
    ],
)
async def test_authority_head_metadata_unavailable_is_local_not_unsafe(
    head: str, key: str
) -> None:
    """A transient exact-key metadata read is not persisted corruption.

    This covers the two readiness bounds that run before model reads.  The
    full admin status must still retain unrelated healthy sources and pairing
    recovery controls rather than turning a one-source outage into a 503.
    """

    app, svc = await _service()
    storage = svc._storage_factory()
    neighbour = "healthy-neighbour"
    await _clone_source_space(storage, neighbour)
    _, invitation = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/invitation",
        body=json.dumps(
            {"confirm": True, "space_id": SPACE, "scopes": ["read"]}
        ).encode(),
    )
    original_list = storage.list_objects

    async def unavailable_head(prefix: str, max_keys: int = 0):
        if prefix == key:
            raise OSError(f"{head} metadata temporarily unavailable")
        return await original_list(prefix, max_keys=max_keys)

    storage.list_objects = unavailable_head
    targeted_status, targeted = await _invoke(
        app, "GET", f"/api/admin/mesh/source-readiness/{SPACE}"
    )
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")

    assert targeted_status == status == 200
    assert targeted["source"]["state"] == "unavailable"
    sources = {item["space_id"]: item for item in payload["source_readiness"]}
    assert sources[SPACE]["state"] == "unavailable"
    assert sources[neighbour]["state"] == "ready"
    assert payload["eligible_spaces"] == [neighbour]
    assert [item["pair_id"] for item in payload["pairings"]] == [
        invitation["pair_id"]
    ]
    assert payload["source_readiness_unavailable"] is False
    assert "temporarily unavailable" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("operation", "key"),
    [
        ("exists", f"{SPACE}/_meta.json"),
        ("get_json", f"{SPACE}/_meta.json"),
        ("get", f"{SPACE}/_rules.md"),
    ],
)
async def test_product_state_read_failure_is_local_unavailable_not_unsafe(
    operation: str, key: str
) -> None:
    """Do not collapse product-marker backend failures into corruption.

    The generic product classifier deliberately gives ordinary callers a safe
    ``unsafe`` answer for every failure.  Mesh readiness retains the actual
    availability taxonomy: only malformed bytes/schema are unsafe; a failed
    exists/get_json/get call leaves this one source unavailable while sibling
    readiness and pairing recovery stay visible.
    """

    app, svc = await _service()
    storage = svc._storage_factory()
    neighbour = "healthy-neighbour"
    await _clone_source_space(storage, neighbour)
    _, invitation = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/invitation",
        body=json.dumps(
            {"confirm": True, "space_id": SPACE, "scopes": ["read"]}
        ).encode(),
    )

    original = getattr(storage, operation)

    if operation == "exists":

        async def unavailable(candidate: str):
            if candidate == key:
                raise OSError("product state backend unavailable")
            return await original(candidate)

    else:

        async def unavailable(candidate: str):
            if candidate == key:
                raise OSError("product state backend unavailable")
            return await original(candidate)

    setattr(storage, operation, unavailable)
    targeted_status, targeted = await _invoke(
        app, "GET", f"/api/admin/mesh/source-readiness/{SPACE}"
    )
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")

    assert targeted_status == status == 200
    assert targeted["source"]["state"] == "unavailable"
    sources = {item["space_id"]: item for item in payload["source_readiness"]}
    assert sources[SPACE]["state"] == "unavailable"
    assert sources[neighbour]["state"] == "ready"
    assert payload["eligible_spaces"] == [neighbour]
    assert [item["pair_id"] for item in payload["pairings"]] == [
        invitation["pair_id"]
    ]
    assert payload["source_readiness_unavailable"] is False
    assert "backend unavailable" not in json.dumps(payload)


async def test_mesh_readiness_uses_the_shared_product_commit_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mesh cannot silently fork SpaceService's product-prefix definition."""

    app, svc = await _service()
    calls: list[tuple[object, str]] = []
    original = SpaceService.inspect_committed_state

    async def tracked(storage: object, space_id: str) -> tuple[str, str]:
        calls.append((storage, space_id))
        return await original(storage, space_id)

    monkeypatch.setattr(
        SpaceService, "inspect_committed_state", staticmethod(tracked)
    )
    status, payload = await _invoke(
        app, "GET", f"/api/admin/mesh/source-readiness/{SPACE}"
    )

    assert status == 200
    assert payload["source"]["state"] == "ready"
    assert calls == [(svc._storage_factory(), SPACE)]


@pytest.mark.parametrize(
    "corruption",
    [
        "invalid_meta_json",
        "wrong_meta_space",
        "invalid_meta_schema",
        "missing_rules",
        "invalid_live_sentinel",
        "invalid_bank_sentinel",
    ],
)
async def test_product_state_corruption_is_local_unsafe_not_unavailable(
    corruption: str,
) -> None:
    """Persisted product corruption is distinct from a transient backend outage."""

    app, svc = await _service()
    storage = svc._storage_factory()
    neighbour = "healthy-neighbour"
    await _clone_source_space(storage, neighbour)
    _, invitation = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/invitation",
        body=json.dumps(
            {"confirm": True, "space_id": SPACE, "scopes": ["read"]}
        ).encode(),
    )
    if corruption == "invalid_meta_json":
        storage.objects[f"{SPACE}/_meta.json"] = "{"
    elif corruption == "wrong_meta_space":
        await storage.put(
            f"{SPACE}/_meta.json", json.dumps({"space_id": "other-space"})
        )
    elif corruption == "invalid_meta_schema":
        await storage.put(f"{SPACE}/_meta.json", json.dumps({"space_id": 7}))
    elif corruption == "missing_rules":
        await storage.delete(f"{SPACE}/_rules.md")
    elif corruption == "invalid_live_sentinel":
        await storage.put(f"{SPACE}/live/.keep", "unexpected")
    else:
        await storage.put(f"{SPACE}/bank/.keep", "unexpected")

    targeted_status, targeted = await _invoke(
        app, "GET", f"/api/admin/mesh/source-readiness/{SPACE}"
    )
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")

    assert targeted_status == status == 200
    source = targeted["source"]
    assert source["state"] == source["hive_status"] == "unsafe"
    assert source["source_ready"] is False
    assert source["source_initializable"] is False
    assert source["can_create_invitation"] is False
    assert source["resumable"] is False
    sources = {item["space_id"]: item for item in payload["source_readiness"]}
    assert sources[SPACE]["state"] == "unsafe"
    assert sources[neighbour]["state"] == "ready"
    assert payload["eligible_spaces"] == [neighbour]
    assert [item["pair_id"] for item in payload["pairings"]] == [
        invitation["pair_id"]
    ]
    assert payload["source_readiness_unavailable"] is False


async def test_status_contains_an_unavailable_entry_for_one_unexpected_source_error() -> None:
    app, svc = await _service()
    storage = svc._storage_factory()
    neighbour = "healthy-neighbour"
    await _clone_source_space(storage, neighbour)
    original_inspect = svc._inspect_source_eligibility

    async def one_source_fails(space_id: str):
        if space_id == SPACE:
            raise OSError("source backend unavailable")
        return await original_inspect(space_id)

    svc._inspect_source_eligibility = one_source_fails
    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")

    assert status == 200
    sources = {item["space_id"]: item for item in payload["source_readiness"]}
    assert sources[SPACE]["state"] == "unavailable"
    assert sources[neighbour]["state"] == "ready"
    assert payload["eligible_spaces"] == [neighbour]
    assert payload["source_readiness_unavailable"] is False
    assert "source backend unavailable" not in json.dumps(payload)


@pytest.mark.parametrize("corruption", ["json", "non_utf8"])
async def test_status_isolates_corrupt_commit_and_preserves_recovery(
    corruption: str,
) -> None:
    app, svc = await _service()
    storage = svc._storage_factory()
    neighbour = "healthy-neighbour"
    await _clone_source_space(storage, neighbour)
    _, invitation = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/invitation",
        body=json.dumps(
            {"confirm": True, "space_id": SPACE, "scopes": ["read"]}
        ).encode(),
    )
    pair_id = invitation["pair_id"]
    session = await svc.store.get_session(pair_id)
    assert session is not None
    session = session.transition(MeshPairingState.CLAIMED, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.APPROVED, now_ms=NOW_MS)
    session = session.transition(MeshPairingState.TRANSFERRING, now_ms=NOW_MS)
    session = session.transition(
        MeshPairingState.BLOCKED_RECOVERY,
        now_ms=NOW_MS,
        last_error="bootstrap_export_failed",
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
    await svc.store.put_evidence(
        pair_id,
        SignedBlockedRecoveryEvidence.sign(evidence, svc._config.private_key),
    )
    marker = "CORRUPT_SELECTED_COMMIT_MUST_NOT_LEAK"
    selected_key = layout.commit_key(SPACE, 1)
    if corruption == "json":
        storage.objects[selected_key] = marker + "{"  # invalid JSON
    else:
        original_get = storage.get

        async def invalid_utf8_get(key: str):
            if key == selected_key:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
            return await original_get(key)

        storage.get = invalid_utf8_get

    status, payload = await _invoke(app, "GET", "/api/admin/mesh/status")

    assert status == 200
    assert payload["status"] == "ok"
    assert [item["pair_id"] for item in payload["pairings"]] == [
        pair_id
    ]
    assert payload["pairings"][0]["next_action"] == "evict"
    assert payload["pairings"][0]["phase"] == "bootstrap_export_failed"
    sources = {item["space_id"]: item for item in payload["source_readiness"]}
    assert sources[SPACE]["state"] == "unsafe"
    assert sources[SPACE]["can_create_invitation"] is False
    assert sources[neighbour]["state"] == "ready"
    assert sources[neighbour]["can_create_invitation"] is True
    assert payload["eligible_spaces"] == [neighbour]
    assert payload["source_readiness_unavailable"] is False
    assert marker not in json.dumps(payload)


async def test_targeted_source_readiness_rejects_invalid_id_and_non_admin() -> None:
    app, _svc = await _service()
    status, payload = await _invoke(
        app,
        "GET",
        "/api/admin/mesh/source-readiness/../etc",
    )
    assert status == 400 and "invalid space id" in payload["message"]

    status, _payload = await _invoke(
        app,
        "GET",
        f"/api/admin/mesh/source-readiness/{SPACE}",
        admin=False,
    )
    assert status == 403


async def test_direct_source_readiness_invalid_id_is_a_non_actionable_projection() -> None:
    """The service boundary also normalizes malformed diagnostic ids."""

    _app, svc = await _service()
    readiness = await svc.inspect_source_eligibility("../not-a-space")

    assert readiness["space_id"] == "../not-a-space"
    assert readiness["state"] == readiness["hive_status"] == "not_a_space"
    assert readiness["source_ready"] is False
    assert readiness["source_initializable"] is False
    assert readiness["can_create_invitation"] is False
    assert readiness["resumable"] is False


async def test_readiness_inventory_and_local_state_failures_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed bounded probe is unavailable; corrupt local pairing state is unsafe."""

    _app, svc = await _service()
    storage = svc._storage_factory()
    original_list = storage.list_objects

    async def unavailable_inventory(prefix: str, max_keys: int = 0):
        if prefix == layout.HIVEMIND_PREFIX(SPACE):
            raise OSError("source inventory unavailable")
        return await original_list(prefix, max_keys=max_keys)

    monkeypatch.setattr(storage, "list_objects", unavailable_inventory)
    unavailable = await svc.inspect_source_eligibility(SPACE)
    assert unavailable["state"] == unavailable["hive_status"] == "unavailable"
    assert unavailable["can_create_invitation"] is False

    monkeypatch.setattr(storage, "list_objects", original_list)

    async def corrupt_reservation(_space_id: str):
        raise MeshPairingStoreError("corrupt_state", "reservation record is invalid")

    monkeypatch.setattr(svc.store, "get_reservation", corrupt_reservation)
    unsafe = await svc.inspect_source_eligibility(SPACE)
    assert unsafe["state"] == unsafe["hive_status"] == "unsafe"
    assert unsafe["can_create_invitation"] is False


@pytest.mark.parametrize(
    ("failure", "expected_state"),
    [
        ("corrupt", "unsafe"),
        ("unavailable", "unavailable"),
    ],
)
async def test_readiness_distinguishes_corrupt_and_unavailable_hivemind_heads(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_state: str,
) -> None:
    """The policy layer must not turn a backend outage into persistent corruption."""

    _app, svc = await _service()
    storage = svc._storage_factory()
    node_key = layout.node_key(SPACE)
    if failure == "corrupt":
        storage.objects[node_key] = "{corrupt node"
    else:
        original_get = storage.get

        async def unavailable_get(key: str):
            if key == node_key:
                raise OSError("critical state unavailable")
            return await original_get(key)

        monkeypatch.setattr(storage, "get", unavailable_get)
    readiness = await svc.inspect_source_eligibility(SPACE)

    assert readiness["state"] == readiness["hive_status"] == expected_state
    assert readiness["source_ready"] is False
    assert readiness["can_create_invitation"] is False


@pytest.mark.parametrize(
    ("blocker", "expected_state"),
    [("reservation", "pairing_in_flight"), ("queue", "busy")],
)
async def test_local_only_readiness_never_offers_prepare_while_blocked(
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
    expected_state: str,
) -> None:
    """A blank committed target must wait for its local blocker to clear."""

    _app, svc, _storage = await _local_service()
    if blocker == "reservation":
        await svc.store.reserve(SPACE, "pair_" + "9" * 32, now_ms=NOW_MS)
    else:

        async def queued(_space_id: str) -> dict:
            return {"queued_count": 1, "running_job_id": ""}

        monkeypatch.setattr(
            svc._consolidation_queue, "get_space_readiness_summary", queued
        )

    readiness = await svc.inspect_source_eligibility(SPACE)
    assert readiness["state"] == expected_state
    assert readiness["source_initializable"] is False
    assert readiness["can_create_invitation"] is False
    assert readiness["resumable"] is False


@pytest.mark.parametrize(
    ("drift", "expected_state"),
    [("node_identity", "identity_mismatch"), ("commit_scope", "insufficient_scope")],
)
async def test_ready_source_identity_and_scope_drift_are_not_invitable(
    drift: str,
    expected_state: str,
) -> None:
    """Valid-schema authority drift remains an explicit, non-actionable state."""

    _app, svc = await _service()
    storage = svc._storage_factory()
    state_store = HivemindStateStore(storage=storage, space_id=SPACE)
    if drift == "node_identity":
        node = await state_store.get_node_identity()
        assert node is not None
        # Raw durable state can be schema-valid while violating the node/member
        # binding.  The readiness projection must not silently repair it.
        await storage.put(
            layout.node_key(SPACE),
            node.model_copy(
                update={"node_id": "driftednode000000000000000000000"}
            ).model_dump_json(),
        )
    else:
        membership = await state_store.get_membership()
        assert membership is not None
        await state_store.set_membership(
            membership.model_copy(
                update={
                    "members": [
                        member.model_copy(update={"scopes": ["read"]})
                        for member in membership.members
                    ]
                }
            )
        )

    readiness = await svc.inspect_source_eligibility(SPACE)
    assert readiness["state"] == expected_state
    assert readiness["source_ready"] is False
    assert readiness["can_create_invitation"] is False


async def test_prepare_source_real_transition_and_idempotent_retry() -> None:
    app, _svc, _storage = await _local_service()
    status, before = await _invoke(app, "GET", "/api/admin/mesh/status")
    source = next(item for item in before["source_readiness"] if item["space_id"] == SPACE)
    assert source["state"] == "local_only_can_prepare"
    assert before["eligible_spaces"] == []

    status, prepared = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/prepare-source",
        body=json.dumps(
            {
                "space_id": SPACE,
                "confirm": True,
                "quiesced": True,
                "expected_state_token": source["state_token"],
            }
        ).encode(),
    )
    assert status == 200
    assert prepared["status"] == "ok"
    assert prepared["result"] == "prepared"
    assert prepared["source"]["state"] == "ready"

    status, retried = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/prepare-source",
        body=json.dumps(
            {
                "space_id": SPACE,
                "confirm": True,
                "quiesced": True,
                "expected_state_token": prepared["source"]["state_token"],
            }
        ).encode(),
    )
    assert status == 200
    assert retried["status"] == "ok"
    assert retried["result"] == "already_ready"
    assert retried["source"]["state"] == "ready"


async def test_prepare_source_stale_token_maps_to_409_without_protocol_write() -> None:
    app, _svc, storage = await _local_service()
    before = storage.snapshot()
    status, payload = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/prepare-source",
        body=json.dumps(
            {
                "space_id": SPACE,
                "confirm": True,
                "quiesced": True,
                "expected_state_token": "0" * 64,
            }
        ).encode(),
    )
    assert status == 409
    assert payload == {
        "status": "error",
        "code": "source_state_changed",
        "message": "source state changed; refresh before preparing",
    }
    assert storage.snapshot() == before


@pytest.mark.parametrize(
    "body",
    [
        {"space_id": SPACE, "quiesced": True, "expected_state_token": "0" * 64},
        {
            "space_id": SPACE,
            "confirm": True,
            "quiesced": False,
            "expected_state_token": "0" * 64,
        },
        {
            "space_id": SPACE,
            "confirm": True,
            "quiesced": True,
            "expected_state_token": "0" * 64,
            "unexpected": True,
        },
    ],
)
async def test_prepare_source_requires_exact_confirmed_quiesced_body(body) -> None:
    app, _svc, storage = await _local_service()
    before = storage.snapshot()
    status, _payload = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/prepare-source",
        body=json.dumps(body).encode(),
    )
    assert status == 400
    assert storage.snapshot() == before


@pytest.mark.parametrize(
    "body",
    [
        {
            "confirm": True,
            "invitation": "aGVsbG8",
            "target_space_id": SPACE,
            "secret": "secret",
            "source_endpoint": "https://a.mesh.test",
            "scopes": ["read"],
        },
        {
            "confirm": True,
            "invitation": "aGVsbG8",
            "target_space_id": SPACE,
            "secret": "secret",
            "source_endpoint": "https://a.mesh.test",
            "scopes": ["read"],
            "quiesced": False,
        },
        {
            "confirm": True,
            "invitation": "aGVsbG8",
            "target_space_id": SPACE,
            "secret": "secret",
            "source_endpoint": "https://a.mesh.test",
            "scopes": ["read"],
            "quiesced": True,
            "unexpected": True,
        },
    ],
)
async def test_accept_requires_exact_confirmed_quiesced_body(body) -> None:
    app, _svc = await _service()
    status, payload = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/accept",
        body=json.dumps(body).encode(),
    )
    assert status == 400
    assert payload["code"] in {"invalid_field", "quiescence_required"}


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


async def test_recover_orphaned_reservation_is_explicit_and_auditable() -> None:
    app, svc = await _service()
    space_id = "recover-target"
    pair_id = "pair_" + "a" * 32
    storage = svc._storage_factory()
    for suffix, payload in (
        ("_meta.json", json.dumps({"space_id": space_id, "version": 1})),
        ("_rules.md", ""),
        ("live/.keep", ""),
        ("bank/.keep", ""),
    ):
        await storage.put(f"{space_id}/{suffix}", payload)
    await svc.store.reserve(space_id, pair_id, now_ms=NOW_MS)

    status, payload = await _invoke(
        app,
        "POST",
        "/api/admin/mesh/recover-orphaned-reservation",
        body=json.dumps(
            {
                "confirm": True,
                "pair_id": pair_id,
                "space_id": space_id,
                "operator": "admin-op",
            }
        ).encode(),
    )

    assert status == 200
    assert payload == {
        "status": "ok",
        "pair_id": pair_id,
        "space_id": space_id,
        "state": "orphaned_reservation_released",
    }
    assert await svc.store.get_reservation(space_id) is None


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
    assert payload["pairing_metadata_truncated"] is False
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
