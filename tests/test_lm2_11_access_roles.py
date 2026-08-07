"""LM2-11 — delegated manage provisioning and strict writer scoping.

The failure-path tests are intentional mutation guards: changing the manage
gate back to write, accepting a hash prefix, moving ``_meta.json`` before the
token grant, or treating an ambiguous S3 write as success makes this module
red.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
from dataclasses import dataclass, field

import pytest
from mcp.server.fastmcp import FastMCP

from live_mem.auth import context as auth_context
from live_mem.core import backup as backup_module
from live_mem.core import locks as locks_module
from live_mem.core import space as space_module
from live_mem.core import tokens as tokens_module
from live_mem.core.backup import BackupService
from live_mem.core.locks import LockManager
from live_mem.core.models import INTERNAL_LONG_TOKEN_NAME, TokenInfo, TokensStore
from live_mem.core.space import SpaceService
from live_mem.core.tokens import TOKENS_KEY, TokenService


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _token(
    name: str,
    char: str,
    *,
    permissions: list[str],
    spaces: list[str] | None = None,
    revoked: bool = False,
    expires_at: str | None = None,
) -> TokenInfo:
    return TokenInfo(
        hash=_hash(char),
        name=name,
        permissions=permissions,
        space_ids=list(spaces or []),
        created_at="2026-07-14T00:00:00+00:00",
        revoked=revoked,
        expires_at=expires_at,
    )


@dataclass
class FakeStorage:
    objects: dict[str, str] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    fail_before_put: set[str] = field(default_factory=set)
    persist_then_raise: set[str] = field(default_factory=set)
    fail_before_delete: set[str] = field(default_factory=set)
    delete_then_raise: set[str] = field(default_factory=set)
    inject_after_delete: dict[str, tuple[str, str]] = field(default_factory=dict)
    fail_get_calls: dict[str, set[int]] = field(default_factory=dict)
    get_counts: dict[str, int] = field(default_factory=dict)
    fail_exists_calls: dict[str, set[int]] = field(default_factory=dict)
    exists_counts: dict[str, int] = field(default_factory=dict)
    pause_put_key: str = ""
    put_started: asyncio.Event | None = None
    release_put: asyncio.Event | None = None

    async def put(
        self, key: str, content: str, content_type: str = "text/plain"
    ) -> None:
        self.events.append(f"put:{key}")
        if key == self.pause_put_key:
            assert self.put_started is not None and self.release_put is not None
            self.put_started.set()
            await self.release_put.wait()
        if key in self.fail_before_put:
            raise OSError(f"put failed before persistence: {key}")
        self.objects[key] = content
        if key in self.persist_then_raise:
            raise TimeoutError(f"ambiguous post-PUT timeout: {key}")

    async def put_json(self, key: str, data: dict) -> None:
        await self.put(
            key,
            json.dumps(data, indent=2, ensure_ascii=False),
            content_type="application/json",
        )

    async def get(self, key: str) -> str | None:
        count = self.get_counts.get(key, 0) + 1
        self.get_counts[key] = count
        if count in self.fail_get_calls.get(key, set()):
            raise TimeoutError(f"read timeout: {key}")
        return self.objects.get(key)

    async def get_json(self, key: str) -> dict | None:
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def exists(self, key: str) -> bool:
        count = self.exists_counts.get(key, 0) + 1
        self.exists_counts[key] = count
        if count in self.fail_exists_calls.get(key, set()):
            raise TimeoutError(f"exists timeout: {key}")
        return key in self.objects

    async def list_objects(self, prefix: str, max_keys: int = 0) -> list[dict]:
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        if max_keys:
            keys = keys[:max_keys]
        return [{"Key": key, "Size": len(self.objects[key])} for key in keys]

    async def delete_many(self, keys: list[str]) -> int:
        self.events.append("delete_many")
        deleted = 0
        for key in keys:
            if key in self.objects:
                del self.objects[key]
                deleted += 1
        return deleted

    async def delete(self, key: str) -> None:
        self.events.append(f"delete:{key}")
        if key in self.fail_before_delete:
            raise OSError(f"delete failed before persistence: {key}")
        self.objects.pop(key, None)
        late = self.inject_after_delete.get(key)
        if late is not None:
            late_key, late_content = late
            self.objects[late_key] = late_content
        if key in self.delete_then_raise:
            raise TimeoutError(f"ambiguous post-DELETE timeout: {key}")

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        self.events.append(f"copy:{source_key}->{dest_key}")
        self.objects[dest_key] = self.objects[source_key]


def _seed_store(storage: FakeStorage, *tokens: TokenInfo) -> None:
    store = TokensStore(tokens=list(tokens))
    storage.objects[TOKENS_KEY] = json.dumps(
        store.model_dump(), indent=2, ensure_ascii=False
    )


def _stored_tokens(storage: FakeStorage) -> TokensStore:
    return TokensStore(**json.loads(storage.objects[TOKENS_KEY]))


def _seed_committed_space(storage: FakeStorage, space_id: str) -> None:
    storage.objects[f"{space_id}/_meta.json"] = json.dumps(
        {"space_id": space_id, "created_at": "2026-07-14T00:00:00+00:00"}
    )
    storage.objects[f"{space_id}/_rules.md"] = "# Rules"
    storage.objects[f"{space_id}/live/.keep"] = ""
    storage.objects[f"{space_id}/bank/.keep"] = ""


@pytest.fixture(autouse=True)
def _isolated_globals(monkeypatch: pytest.MonkeyPatch):
    manager = LockManager()
    monkeypatch.setattr(locks_module, "_lock_manager", manager)
    auth_context._fresh_token_store.clear()
    auth_context._invalidated_token_hashes.clear()
    yield
    auth_context._fresh_token_store.clear()
    auth_context._invalidated_token_hashes.clear()


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    storage = FakeStorage()
    token_service = TokenService()
    monkeypatch.setattr(tokens_module, "get_storage", lambda: storage)
    monkeypatch.setattr(space_module, "get_storage", lambda: storage)
    monkeypatch.setattr(backup_module, "get_storage", lambda: storage)
    monkeypatch.setattr(tokens_module, "_token_service", token_service)
    return storage, token_service


def _handler(register, name: str):
    mcp = FastMCP(name=f"lm2-11-{name}")
    register(mcp)
    return mcp._tool_manager._tools[name].fn


@pytest.mark.asyncio
async def test_writer_cannot_create_space_or_token(wired):
    """Mutation manage→write turns this behavioral guard red."""
    from live_mem.tools.access import register as register_access
    from live_mem.tools.space import register as register_space

    storage, _ = wired
    writer = {
        "type": "token",
        "client_name": "writer",
        "permissions": ["read", "write"],
        "allowed_resources": ["alpha"],
        "token_hash": _hash("a"),
    }
    token = auth_context.current_token_info.set(writer)
    auth_context.update_fresh_token(writer)
    try:
        create_space = _handler(register_space, "space_create")
        create_token = _handler(register_access, "token_create")
        invite_token = _handler(register_access, "space_invite_token")
        space_result = await create_space("new-space", "desc", "# Rules")
        token_result = await create_token("reader", "read")
        invite_result = await invite_token("alpha", _hash("b"))
    finally:
        auth_context.current_token_info.reset(token)

    assert space_result["status"] == "error"
    assert token_result["status"] == "error"
    assert invite_result["status"] == "error"
    assert storage.objects == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("bootstrap", [False, True])
@pytest.mark.parametrize("recover_access_grants", [False, True])
async def test_space_delete_passes_exact_persisted_actor_identity(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    bootstrap: bool,
    recover_access_grants: bool,
):
    from live_mem.tools.space import register as register_space

    captured: dict = {}

    class _Spaces:
        async def delete(self, space_id: str, **kwargs):
            captured["space_id"] = space_id
            captured.update(kwargs)
            return {"status": "deleted"}

    monkeypatch.setattr(space_module, "get_space_service", lambda: _Spaces())
    identity = {
        "type": "bootstrap" if bootstrap else "token",
        "client_name": "operator",
        "permissions": ["admin"] if bootstrap else ["manage"],
        "allowed_resources": [] if bootstrap else ["alpha"],
    }
    if not bootstrap:
        identity["token_hash"] = _hash("a")
        auth_context.update_fresh_token(identity)
    token = auth_context.current_token_info.set(identity)
    try:
        result = await _handler(register_space, "space_delete")(
            "alpha",
            confirm=True,
            recover_access_grants=recover_access_grants,
        )
    finally:
        auth_context.current_token_info.reset(token)

    assert result["status"] == "deleted"
    assert captured == {
        "space_id": "alpha",
        "unsafe_recovery": False,
        "recover_access_grants": recover_access_grants,
        "actor_token_hash": "" if bootstrap else _hash("a"),
        "bootstrap_admin": bootstrap,
    }


@pytest.mark.asyncio
async def test_delegated_token_create_is_actor_aware_and_audited(
    wired, caplog: pytest.LogCaptureFixture
):
    storage, service = wired
    manager = _token(
        "manager", "a", permissions=["read", "write", "manage"], spaces=[]
    )
    _seed_store(storage, manager)

    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        result = await service.create_delegated_token(
            actor_token_hash=manager.hash,
            name="team-manager",
            permissions="read,write,manage",
            expires_in_days=7,
            email="owner@example.test",
        )

    assert result["status"] == "created"
    assert re.fullmatch(r"lm_[A-Za-z0-9_-]{43}", result["token"])
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", result["token_hash"])
    assert result["permissions"] == ["read", "write", "manage"]
    assert result["space_ids"] == []
    created = _stored_tokens(storage).tokens[-1]
    assert created.hash == result["token_hash"]
    assert created.space_ids == []
    audit = json.loads(caplog.records[-1].message)
    assert audit["event"] == "token_create"
    assert audit["caller"] == "manager"
    assert audit["actor_token_hash"] == manager.hash
    assert audit["token_hash"] == result["token_hash"]
    assert "token" not in audit


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_state", ["revoked", "expired", "downgraded"])
async def test_token_create_revalidates_before_generating_secret(
    wired, monkeypatch: pytest.MonkeyPatch, actor_state: str
):
    storage, service = wired
    actor = _token(
        "former-manager",
        "a",
        permissions=["read", "write", "manage"],
    )
    if actor_state == "revoked":
        actor.revoked = True
    elif actor_state == "expired":
        actor.expires_at = "2000-01-01T00:00:00+00:00"
    else:
        actor.permissions = ["read", "write"]
    _seed_store(storage, actor)
    generated = 0

    def _secret(_: int) -> str:
        nonlocal generated
        generated += 1
        return "x" * 43

    monkeypatch.setattr(tokens_module.secrets, "token_urlsafe", _secret)
    before = dict(storage.objects)
    result = await service.create_delegated_token(
        actor_token_hash=actor.hash,
        name="reader",
        permissions="read",
    )
    assert result["status"] == "error"
    assert generated == 0
    assert storage.objects == before


@pytest.mark.asyncio
async def test_delegated_token_create_rejects_overflow_expiry_before_secret(
    wired, monkeypatch: pytest.MonkeyPatch
):
    storage, service = wired
    manager = _token("manager", "a", permissions=["read", "write", "manage"])
    _seed_store(storage, manager)
    generated = 0

    def _secret(_: int) -> str:
        nonlocal generated
        generated += 1
        return "x" * 43

    monkeypatch.setattr(tokens_module.secrets, "token_urlsafe", _secret)
    before = dict(storage.objects)
    result = await service.create_delegated_token(
        actor_token_hash=manager.hash,
        name="reader",
        permissions="read",
        expires_in_days=10**20,
    )
    assert result["status"] == "error"
    assert generated == 0
    assert storage.objects == before


@pytest.mark.asyncio
async def test_token_create_revalidates_permission_after_waiting_for_lock(
    wired, monkeypatch: pytest.MonkeyPatch
):
    """A request queued as manage must observe a downgrade before mutation."""
    storage, service = wired
    manager = _token("manager", "a", permissions=["read", "write", "manage"])
    _seed_store(storage, manager)
    generated = 0

    def _secret(_: int) -> str:
        nonlocal generated
        generated += 1
        return "x" * 43

    monkeypatch.setattr(tokens_module.secrets, "token_urlsafe", _secret)
    lock = locks_module.get_lock_manager().tokens
    await lock.acquire()
    try:
        pending = asyncio.create_task(
            service.create_delegated_token(
                actor_token_hash=manager.hash,
                name="reader",
                permissions="read",
            )
        )
        await asyncio.sleep(0)
        downgraded = _stored_tokens(storage)
        downgraded.tokens[0].permissions = ["read", "write"]
        storage.objects[TOKENS_KEY] = json.dumps(downgraded.model_dump())
    finally:
        lock.release()

    result = await pending
    assert result["status"] == "error"
    assert generated == 0
    assert len(_stored_tokens(storage).tokens) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "permissions"),
    [
        ("normal", "read,write,admin"),
        ("normal", "write,read"),
        ("normal", "read, write"),
        (INTERNAL_LONG_TOKEN_NAME, "read"),
    ],
)
async def test_token_create_rejects_noncanonical_or_reserved_without_secret(
    wired, monkeypatch: pytest.MonkeyPatch, name: str, permissions: str
):
    storage, service = wired
    manager = _token("manager", "a", permissions=["manage"])
    _seed_store(storage, manager)
    generated = 0

    def _secret(_: int) -> str:
        nonlocal generated
        generated += 1
        return "x" * 43

    monkeypatch.setattr(tokens_module.secrets, "token_urlsafe", _secret)
    result = await service.create_delegated_token(
        actor_token_hash=manager.hash,
        name=name,
        permissions=permissions,
    )
    assert result["status"] == "error"
    assert generated == 0
    assert len(_stored_tokens(storage).tokens) == 1


@pytest.mark.asyncio
async def test_token_create_ambiguous_save_returns_secret_as_partial_without_audit(
    wired, caplog: pytest.LogCaptureFixture
):
    storage, service = wired
    manager = _token("manager", "a", permissions=["manage"])
    _seed_store(storage, manager)
    storage.persist_then_raise.add(TOKENS_KEY)
    # call 1 = initial actor load; call 2 = confirmation reload
    storage.fail_get_calls[TOKENS_KEY] = {2}

    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        result = await service.create_delegated_token(
            actor_token_hash=manager.hash,
            name="reader",
            permissions="read",
        )

    assert result["status"] == "partial"
    assert result["recovery_required"] is True
    assert result["token"].startswith("lm_")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", result["token_hash"])
    assert not [r for r in caplog.records if '"event": "token_create"' in r.message]


@pytest.mark.asyncio
async def test_token_store_v1_migrates_once_and_v2_never_rewidens_empty_scope(wired):
    storage, service = wired
    legacy_writer = _token("legacy", "a", permissions=["read", "write"])
    legacy_admin = _token(
        "admin", "b", permissions=["admin"], spaces=["alpha"]
    )
    revoked_admin = _token(
        "revoked-admin",
        "c",
        permissions=["admin"],
        spaces=["beta"],
        revoked=True,
    )
    legacy = TokensStore(
        version=1, tokens=[legacy_writer, legacy_admin, revoked_admin]
    )
    storage.objects[TOKENS_KEY] = json.dumps(legacy.model_dump())

    first = await service.migrate_empty_space_ids(["alpha", "beta"])
    assert first["migrated"] == 1
    assert first["admin_scopes_cleared"] == 2
    migrated = _stored_tokens(storage)
    assert migrated.version == tokens_module.CURRENT_TOKENS_VERSION
    assert migrated.tokens[0].space_ids == ["alpha", "beta"]
    assert migrated.tokens[1].space_ids == []
    assert migrated.tokens[2].space_ids == []

    snapshot = storage.objects[TOKENS_KEY]
    second = await service.migrate_empty_space_ids(["gamma"])
    assert second["already_migrated"] is True
    assert storage.objects[TOKENS_KEY] == snapshot

    # A new v2 token with [] means exactly no access across future startups.
    migrated.tokens.append(_token("new-reader", "d", permissions=["read"]))
    storage.objects[TOKENS_KEY] = json.dumps(migrated.model_dump())
    third = await service.migrate_empty_space_ids(["alpha", "beta", "gamma"])
    assert third["already_migrated"] is True
    assert _stored_tokens(storage).tokens[-1].space_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [0, 3, "2", True])
async def test_token_store_unknown_or_corrupt_version_fails_closed(wired, version):
    storage, service = wired
    storage.objects[TOKENS_KEY] = json.dumps({"version": version, "tokens": []})
    with pytest.raises(RuntimeError, match="[Vv]ersion|corrompu"):
        await service._load_store()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "token_not_object",
        "bare_hash",
        "uppercase_hash",
        "duplicate_hash",
        "permissions_not_list",
        "permission_not_string",
        "unknown_permission",
        "duplicate_permission",
        "spaces_not_list",
        "space_not_string",
        "invalid_space",
        "duplicate_space",
        "admin_scope",
        "revoked_not_bool",
        "expires_not_string",
    ],
)
async def test_token_store_critical_fields_fail_closed_without_mutation(wired, case):
    storage, service = wired
    entry = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    raw_entry = entry.model_dump()
    raw_tokens: list[object] = [raw_entry]

    if case == "token_not_object":
        raw_tokens = ["not-an-object"]
    elif case == "bare_hash":
        raw_entry["hash"] = "a" * 64
    elif case == "uppercase_hash":
        raw_entry["hash"] = "sha256:" + "A" * 64
    elif case == "duplicate_hash":
        duplicate = dict(raw_entry)
        duplicate["name"] = "duplicate"
        raw_tokens.append(duplicate)
    elif case == "permissions_not_list":
        raw_entry["permissions"] = "manage"
    elif case == "permission_not_string":
        raw_entry["permissions"] = ["manage", 7]
    elif case == "unknown_permission":
        raw_entry["permissions"] = ["owner"]
    elif case == "duplicate_permission":
        raw_entry["permissions"] = ["manage", "manage"]
    elif case == "spaces_not_list":
        raw_entry["space_ids"] = "alpha"
    elif case == "space_not_string":
        raw_entry["space_ids"] = ["alpha", 7]
    elif case == "invalid_space":
        raw_entry["space_ids"] = ["../alpha"]
    elif case == "duplicate_space":
        raw_entry["space_ids"] = ["alpha", "alpha"]
    elif case == "admin_scope":
        raw_entry["permissions"] = ["admin"]
        raw_entry["space_ids"] = ["alpha"]
    elif case == "revoked_not_bool":
        raw_entry["revoked"] = "false"
    elif case == "expires_not_string":
        raw_entry["expires_at"] = 123

    storage.objects[TOKENS_KEY] = json.dumps(
        {"version": 2, "tokens": raw_tokens}
    )
    before = storage.objects[TOKENS_KEY]
    with pytest.raises(RuntimeError, match="corrompu"):
        await service._load_store()
    assert storage.objects[TOKENS_KEY] == before


@pytest.mark.asyncio
async def test_token_store_accepts_known_noninclusive_permission_profiles(wired):
    storage, service = wired
    manage_only = _token("manager", "a", permissions=["manage"])
    write_only = _token("writer", "b", permissions=["write"])
    _seed_store(storage, manage_only, write_only)
    loaded = await service._load_store()
    assert [token.permissions for token in loaded.tokens] == [["manage"], ["write"]]


@pytest.mark.asyncio
async def test_legacy_migration_validates_payload_before_writing_v2_marker(wired):
    storage, service = wired
    legacy = _token("legacy", "a", permissions=["read"])
    raw = legacy.model_dump()
    raw["space_ids"] = ["alpha", "alpha"]
    storage.objects[TOKENS_KEY] = json.dumps({"version": 1, "tokens": [raw]})
    before = storage.objects[TOKENS_KEY]

    with pytest.raises(RuntimeError, match="dupliqué"):
        await service.migrate_empty_space_ids(["alpha"])
    assert storage.objects[TOKENS_KEY] == before


@pytest.mark.asyncio
async def test_save_store_validates_before_any_put(wired):
    storage, service = wired
    invalid = _token("invalid", "a", permissions=["read"])
    invalid.permissions = ["read", "read"]
    store = TokensStore(tokens=[invalid])

    with pytest.raises(RuntimeError, match="dupliqué"):
        await service._save_store(store)
    assert TOKENS_KEY not in storage.objects


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("permissions", "space_ids"),
    [("read,read", ""), ("read", "alpha,alpha"), ("read", "../alpha")],
)
async def test_historical_token_create_rejects_invalid_lists_before_secret(
    wired, monkeypatch: pytest.MonkeyPatch, permissions: str, space_ids: str
):
    storage, service = wired
    generated = 0

    def _secret(_: int) -> str:
        nonlocal generated
        generated += 1
        return "x" * 43

    monkeypatch.setattr(tokens_module.secrets, "token_urlsafe", _secret)
    result = await service.create_token("admin-created", permissions, space_ids)
    assert result["status"] == "error"
    assert generated == 0
    assert storage.objects == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expires_in_days",
    [-1, True, 1.5, "7", 10**20],
)
async def test_historical_token_create_rejects_invalid_expiry_before_secret(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    expires_in_days,
):
    storage, service = wired
    generated = 0

    def _secret(_: int) -> str:
        nonlocal generated
        generated += 1
        return "x" * 43

    monkeypatch.setattr(tokens_module.secrets, "token_urlsafe", _secret)
    result = await service.create_token(
        "admin-created",
        "read",
        expires_in_days=expires_in_days,
    )
    assert result["status"] == "error"
    assert generated == 0
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_admin_create_discards_scope_and_downgrade_cannot_activate_it(wired):
    """Admin allowlists are never persisted as dormant post-downgrade grants."""
    storage, service = wired
    _seed_store(storage)

    created = await service.create_token(
        "admin-created", "read,write,manage,admin", "alpha"
    )
    assert created["status"] == "created"
    assert created["space_ids"] == []
    assert created["scope_normalized"] is True
    assert "scope dormant" in created["info"]
    stored = _stored_tokens(storage).tokens[0]
    assert stored.space_ids == []

    space = await SpaceService().create(
        "alpha", "description", "# Rules", bootstrap_admin=True
    )
    assert space["status"] == "created"

    downgraded = await service.update_token(
        created["token_hash"], permissions="read"
    )
    assert downgraded["status"] == "ok"
    assert "warning_no_access" in downgraded
    stored = _stored_tokens(storage).tokens[0]
    assert stored.permissions == ["read"]
    assert stored.space_ids == []
    assert auth_context._evaluate_access(
        {
            "permissions": stored.permissions,
            "allowed_resources": stored.space_ids,
            "token_hash": stored.hash,
        },
        "alpha",
    )["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_admin_downgrade_delta_starts_from_empty_not_dormant_scope(
    wired, monkeypatch: pytest.MonkeyPatch, bulk: bool
):
    """Mutation guard for legacy/admin scopes on single and bulk updates."""
    _storage, service = wired
    legacy_admin = _token(
        "legacy-admin", "a", permissions=["admin"], spaces=["alpha"]
    )
    store = TokensStore(tokens=[legacy_admin])

    async def _load_legacy_for_transition():
        # Simule l'objet déjà chargé par un ancien runtime avant le marker v2.
        return store

    async def _save_transition(updated):
        # La post-condition doit être un v2 valide avant toute persistance.
        service._store_from_data(updated.model_dump())

    monkeypatch.setattr(service, "_load_store", _load_legacy_for_transition)
    monkeypatch.setattr(service, "_save_store", _save_transition)

    if bulk:
        result = await service.bulk_update_tokens(
            names="legacy-admin",
            permissions="read",
            space_ids_add="beta",
        )
    else:
        result = await service.update_token(
            legacy_admin.hash,
            permissions="read",
            space_ids_add="beta",
        )

    assert result["status"] == "ok"
    assert legacy_admin.permissions == ["read"]
    assert legacy_admin.space_ids == ["beta"]
    transition = result["tokens"][0] if bulk else result
    assert transition["space_ids_removed"] == ["alpha"]
    assert transition["space_ids_added"] == ["beta"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_admin_promotion_clears_existing_non_admin_scope(wired, bulk: bool):
    storage, service = wired
    reader = _token("reader", "a", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, reader)

    if bulk:
        updated = await service.bulk_update_tokens(
            names="reader", permissions="read,admin"
        )
    else:
        updated = await service.update_token(
            reader.hash, permissions="read,admin"
        )
    assert updated["status"] == "ok"
    stored = _stored_tokens(storage).tokens[0]
    assert "admin" in stored.permissions
    assert stored.space_ids == []


@pytest.mark.asyncio
async def test_historical_token_create_pre_put_failure_returns_no_orphan_secret(wired):
    storage, service = wired
    _seed_store(storage)
    before = storage.objects[TOKENS_KEY]
    storage.fail_before_put.add(TOKENS_KEY)

    result = await service.create_token("reader", "read")
    assert result["status"] == "error"
    assert "token" not in result
    assert storage.objects[TOKENS_KEY] == before


@pytest.mark.asyncio
async def test_historical_token_create_post_put_timeout_reprobes_to_created(wired):
    storage, service = wired
    _seed_store(storage)
    storage.persist_then_raise.add(TOKENS_KEY)

    result = await service.create_token("reader", "read")
    assert result["status"] == "created"
    assert _stored_tokens(storage).tokens[0].hash == result["token_hash"]


@pytest.mark.asyncio
async def test_historical_token_create_read_ambiguity_returns_secret_as_partial(wired):
    storage, service = wired
    _seed_store(storage)
    storage.persist_then_raise.add(TOKENS_KEY)
    # call 1 = initial load; call 2 = confirmation after ambiguous PUT
    storage.fail_get_calls[TOKENS_KEY] = {2}

    result = await service.create_token("reader", "read")
    assert result["status"] == "partial"
    assert result["recovery_required"] is True
    assert result["token"].startswith("lm_")
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", result["token_hash"])
    assert _stored_tokens(storage).tokens[0].hash == result["token_hash"]


@pytest.mark.asyncio
async def test_historical_token_create_conflicting_reprobe_returns_partial(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    service = TokenService()
    secret_body = "x" * 43
    raw_token = "lm_" + secret_body
    token_hash = "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()
    conflict = TokenInfo(
        hash=token_hash,
        name="conflicting-record",
        permissions=["read"],
        space_ids=[],
        created_at="2000-01-01T00:00:00+00:00",
    )
    loads = iter([TokensStore(), TokensStore(tokens=[conflict])])

    async def _load():
        return next(loads)

    async def _save(_store):
        raise TimeoutError("ambiguous PUT")

    monkeypatch.setattr(tokens_module.secrets, "token_urlsafe", lambda _: secret_body)
    monkeypatch.setattr(service, "_load_store", _load)
    monkeypatch.setattr(service, "_save_store", _save)
    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        result = await service.create_token("reader", "read")

    assert result["status"] == "partial"
    assert result["recovery_required"] is True
    assert result["token"] == raw_token
    assert result["token_hash"] == token_hash
    assert not [record for record in caplog.records if "token_create" in record.message]


@pytest.mark.asyncio
async def test_empty_store_migration_writes_durable_v2_marker_even_without_spaces(wired):
    storage, service = wired
    result = await service.migrate_empty_space_ids([])
    assert result["already_migrated"] is False
    assert _stored_tokens(storage).version == tokens_module.CURRENT_TOKENS_VERSION


@pytest.mark.asyncio
async def test_asgi_lifespan_runs_migration_even_with_zero_spaces(
    monkeypatch: pytest.MonkeyPatch,
):
    """Factory deployments must not skip the one-shot auth migration."""
    from live_mem import server
    from live_mem.core import consolidator as consolidator_module
    from live_mem.core import embedded_secret as embedded_secret_module

    calls: list[list[str]] = []
    registrations: list[str] = []

    class _Spaces:
        async def list_spaces(self):
            return {"status": "ok", "spaces": []}

    class _Tokens:
        async def migrate_empty_space_ids(self, ids):
            calls.append(list(ids))
            return {"status": "ok", "migrated": 0, "already_migrated": False}

        async def register_internal_long_token(self, token):
            registrations.append(token)
            return {
                "status": "ok",
                "registered": True,
                "current_active": True,
                "rotated_out": 0,
            }

    async def _close():
        return None

    monkeypatch.setattr(space_module, "get_space_service", lambda: _Spaces())
    monkeypatch.setattr(tokens_module, "get_token_service", lambda: _Tokens())
    monkeypatch.setattr(
        embedded_secret_module,
        "resolve_embedded_token",
        lambda *_args, **_kwargs: "durable-embedded-token",
    )
    monkeypatch.setattr(consolidator_module, "close_consolidator_if_initialized", _close)
    async with server._lifespan(None):
        pass
    assert calls == [[]]
    assert registrations == ["durable-embedded-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["space_list", "migration"])
async def test_asgi_lifespan_fails_closed_before_serving_on_migration_error(
    monkeypatch: pytest.MonkeyPatch, failure_at: str
):
    from live_mem import server

    migration_calls = 0

    class _Spaces:
        async def list_spaces(self):
            if failure_at == "space_list":
                return {"status": "error", "message": "S3 unavailable"}
            return {"status": "ok", "spaces": []}

    class _Tokens:
        async def migrate_empty_space_ids(self, _ids):
            nonlocal migration_calls
            migration_calls += 1
            raise RuntimeError("migration write failed")

    monkeypatch.setattr(space_module, "get_space_service", lambda: _Spaces())
    monkeypatch.setattr(tokens_module, "get_token_service", lambda: _Tokens())

    with pytest.raises(RuntimeError, match="migr|lister"):
        async with server._lifespan(None):
            pytest.fail("lifespan must not yield after migration failure")
    assert migration_calls == (0 if failure_at == "space_list" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["secret", "inactive", "registration"])
async def test_asgi_lifespan_embedded_preflight_fails_before_serving(
    monkeypatch: pytest.MonkeyPatch, failure: str
):
    from live_mem import server
    from live_mem.core import embedded_secret as embedded_secret_module

    class _Spaces:
        async def list_spaces(self):
            return {"status": "ok", "spaces": []}

    class _Tokens:
        async def migrate_empty_space_ids(self, _ids):
            return {"status": "ok", "migrated": 0, "already_migrated": True}

        async def register_internal_long_token(self, _token):
            if failure == "registration":
                return {"status": "error", "message": "S3 unavailable"}
            return {
                "status": "ok",
                "registered": False,
                "current_active": failure != "inactive",
                "rotated_out": 0,
            }

    monkeypatch.setattr(space_module, "get_space_service", lambda: _Spaces())
    monkeypatch.setattr(tokens_module, "get_token_service", lambda: _Tokens())
    monkeypatch.setattr(
        embedded_secret_module,
        "resolve_embedded_token",
        lambda *_args, **_kwargs: None if failure == "secret" else "stable-token",
    )
    with pytest.raises(RuntimeError, match="Secret|Token") as error:
        async with server._lifespan(None):
            pytest.fail("lifespan must not yield after embedded preflight failure")
    if failure == "secret":
        assert "LONG_EMBEDDED_TOKEN" in str(error.value)
        assert "docs/DEPLOYMENT.md" in str(error.value)


@pytest.mark.asyncio
async def test_asgi_lifespan_retry_reuses_persisted_plaintext(
    monkeypatch: pytest.MonkeyPatch,
):
    from live_mem import server
    from live_mem.core import consolidator as consolidator_module
    from live_mem.core import embedded_secret as embedded_secret_module

    resolved: list[str] = []
    registered: list[str] = []

    class _Spaces:
        async def list_spaces(self):
            return {"status": "ok", "spaces": []}

    class _Tokens:
        async def migrate_empty_space_ids(self, _ids):
            return {"status": "ok", "migrated": 0, "already_migrated": True}

        async def register_internal_long_token(self, token):
            registered.append(token)
            if len(registered) == 1:
                return {"status": "error", "message": "ambiguous PUT"}
            return {
                "status": "ok",
                "registered": False,
                "current_active": True,
                "rotated_out": 0,
            }

    def _resolve(*_args, **_kwargs):
        resolved.append("same-persisted-token")
        return resolved[-1]

    async def _close():
        return None

    tokens = _Tokens()
    monkeypatch.setattr(space_module, "get_space_service", lambda: _Spaces())
    monkeypatch.setattr(tokens_module, "get_token_service", lambda: tokens)
    monkeypatch.setattr(embedded_secret_module, "resolve_embedded_token", _resolve)
    monkeypatch.setattr(consolidator_module, "close_consolidator_if_initialized", _close)

    with pytest.raises(RuntimeError, match="Token"):
        async with server._lifespan(None):
            pytest.fail("first registration attempt must block startup")
    async with server._lifespan(None):
        pass
    assert resolved == ["same-persisted-token", "same-persisted-token"]
    assert registered == ["same-persisted-token", "same-persisted-token"]


def test_delegated_tool_schema_advertises_closed_profiles_and_exact_hash_contract():
    from live_mem.tools.access import register as register_access

    mcp = FastMCP(name="lm2-11-schema")
    register_access(mcp)
    token_schema = mcp._tool_manager._tools["token_create"].parameters
    invite_schema = mcp._tool_manager._tools["space_invite_token"].parameters
    assert token_schema["properties"]["permissions"]["enum"] == [
        "read",
        "read,write",
        "read,write,manage",
    ]
    hash_schema = invite_schema["properties"]["token_hash"]
    assert "sha256:" in hash_schema["description"]
    assert "64" in hash_schema["description"]
    # No schema regex: malformed hashes must reach the handler's uniform opaque
    # response instead of leaking a distinct FastMCP validation error.
    assert "pattern" not in hash_schema


def test_admin_tool_schemas_publish_manage_profiles():
    """Bootstrap/admin clients discover manager creation via list_tools."""
    from live_mem.tools.admin import register as register_admin

    mcp = FastMCP(name="lm2-11-admin-schema")
    register_admin(mcp)
    for tool_name in (
        "admin_create_token",
        "admin_update_token",
        "admin_bulk_update_tokens",
    ):
        description = mcp._tool_manager._tools[tool_name].parameters[
            "properties"
        ]["permissions"]["description"]
        assert "read,write,manage" in description
        assert "read,write,manage,admin" in description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["malformed", "uppercase", "unknown", "revoked", "expired", "admin", "internal"],
)
async def test_invite_uses_exact_hash_and_one_opaque_ineligible_error(wired, case: str):
    storage, service = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    target = _token("target", "b", permissions=["read"])
    if case == "revoked":
        target.revoked = True
    elif case == "expired":
        target.expires_at = "2000-01-01T00:00:00+00:00"
    elif case == "admin":
        target.permissions = ["admin"]
    elif case == "internal":
        target.name = INTERNAL_LONG_TOKEN_NAME
    _seed_store(storage, manager, target)
    _seed_committed_space(storage, "alpha")
    requested = {
        "malformed": target.hash.removeprefix("sha256:"),
        "uppercase": "sha256:" + "B" * 64,
        "unknown": _hash("c"),
    }.get(case, target.hash)

    before = storage.objects[TOKENS_KEY]
    result = await service.invite_token_to_space(
        actor_token_hash=manager.hash,
        space_id="alpha",
        target_token_hash=requested,
    )
    assert result == {"status": "error", "message": "Token cible non invitable"}
    assert storage.objects[TOKENS_KEY] == before


@pytest.mark.asyncio
async def test_invite_is_add_only_idempotent_invalidate_after_save_and_audited(
    wired, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    storage, service = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    target = _token("target", "b", permissions=["read"])
    _seed_store(storage, manager, target)
    _seed_committed_space(storage, "alpha")
    invalidated: list[list[str]] = []
    monkeypatch.setattr(
        service,
        "_invalidate_in_fresh_store",
        lambda hashes: invalidated.append(list(hashes)),
    )

    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        first = await service.invite_token_to_space(
            actor_token_hash=manager.hash,
            space_id="alpha",
            target_token_hash=target.hash,
        )
        second = await service.invite_token_to_space(
            actor_token_hash=manager.hash,
            space_id="alpha",
            target_token_hash=target.hash,
        )

    assert first == {
        "status": "ok",
        "space_id": "alpha",
        "added": True,
        "mcp_reconnect_required": True,
    }
    assert second == {"status": "ok", "space_id": "alpha", "added": False}
    assert _stored_tokens(storage).tokens[1].space_ids == ["alpha"]
    assert invalidated == [[target.hash]]
    audits = [json.loads(r.message) for r in caplog.records if r.name == "live_mem.audit"]
    assert [a["added"] for a in audits[-2:]] == [True, False]
    assert all(a["target_token_hash"] == target.hash for a in audits[-2:])


@pytest.mark.asyncio
async def test_invite_revalidates_caller_scope_without_mutation(wired):
    storage, service = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["other"])
    target = _token("target", "b", permissions=["read"])
    _seed_store(storage, manager, target)
    _seed_committed_space(storage, "alpha")
    before = storage.objects[TOKENS_KEY]
    result = await service.invite_token_to_space(
        actor_token_hash=manager.hash,
        space_id="alpha",
        target_token_hash=target.hash,
    )
    assert result["status"] == "error"
    assert storage.objects[TOKENS_KEY] == before


@pytest.mark.asyncio
async def test_invite_revalidates_scope_after_waiting_for_token_lock(wired):
    """A queued invitation cannot use scope removed while it waited."""
    storage, service = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    target = _token("target", "b", permissions=["read"])
    _seed_store(storage, manager, target)
    _seed_committed_space(storage, "alpha")

    lock = locks_module.get_lock_manager().tokens
    await lock.acquire()
    try:
        pending = asyncio.create_task(
            service.invite_token_to_space(
                actor_token_hash=manager.hash,
                space_id="alpha",
                target_token_hash=target.hash,
            )
        )
        await asyncio.sleep(0)
        rescoped = _stored_tokens(storage)
        rescoped.tokens[0].space_ids = []
        storage.objects[TOKENS_KEY] = json.dumps(rescoped.model_dump())
    finally:
        lock.release()

    result = await pending
    assert result["status"] == "error"
    assert _stored_tokens(storage).tokens[1].space_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous_read", [False, True])
async def test_invite_save_failure_never_invalidates_or_audits_unconfirmed(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ambiguous_read: bool,
):
    storage, service = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    target = _token("target", "b", permissions=["read"])
    _seed_store(storage, manager, target)
    _seed_committed_space(storage, "alpha")
    storage.fail_before_put.add(TOKENS_KEY)
    if ambiguous_read:
        # actor load = 1; confirmation after failed save = 2
        storage.fail_get_calls[TOKENS_KEY] = {2}
    invalidated: list[list[str]] = []
    monkeypatch.setattr(
        service,
        "_invalidate_in_fresh_store",
        lambda hashes: invalidated.append(list(hashes)),
    )

    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        result = await service.invite_token_to_space(
            actor_token_hash=manager.hash,
            space_id="alpha",
            target_token_hash=target.hash,
        )
    assert result["status"] == ("partial" if ambiguous_read else "error")
    assert invalidated == []
    assert not [
        r for r in caplog.records if '"event": "space_invite_token"' in r.message
    ]


@pytest.mark.asyncio
async def test_concurrent_invites_preserve_both_updates(wired):
    storage, service = wired
    manager = _token(
        "manager", "a", permissions=["manage"], spaces=["alpha", "beta"]
    )
    target_a = _token("target-a", "b", permissions=["read"])
    target_b = _token("target-b", "c", permissions=["read"])
    _seed_store(storage, manager, target_a, target_b)
    _seed_committed_space(storage, "alpha")
    _seed_committed_space(storage, "beta")

    results = await asyncio.gather(
        service.invite_token_to_space(
            actor_token_hash=manager.hash,
            space_id="alpha",
            target_token_hash=target_a.hash,
        ),
        service.invite_token_to_space(
            actor_token_hash=manager.hash,
            space_id="beta",
            target_token_hash=target_b.hash,
        ),
    )
    assert all(result["status"] == "ok" for result in results)
    by_name = {token.name: token for token in _stored_tokens(storage).tokens}
    assert by_name["target-a"].space_ids == ["alpha"]
    assert by_name["target-b"].space_ids == ["beta"]


@pytest.mark.asyncio
async def test_invite_refuses_corrupt_committed_space_without_token_save(wired):
    storage, service = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    target = _token("target", "b", permissions=["read"])
    _seed_store(storage, manager, target)
    storage.objects["alpha/_meta.json"] = "{corrupt"
    before = storage.objects[TOKENS_KEY]
    result = await service.invite_token_to_space(
        actor_token_hash=manager.hash,
        space_id="alpha",
        target_token_hash=target.hash,
    )
    assert result["status"] == "error"
    assert result["recovery_required"] is True
    assert storage.objects[TOKENS_KEY] == before


@pytest.mark.asyncio
async def test_space_create_orders_prepare_grant_then_meta_and_confirms(
    wired, caplog: pytest.LogCaptureFixture
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"])
    _seed_store(storage, manager)
    storage.events.clear()

    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        result = await SpaceService().create(
            "alpha",
            "description",
            "# Rules\n",
            owner="owner",
            actor_token_hash=manager.hash,
        )

    assert result["status"] == "created"
    assert result["token_auto_updated"] is True
    order = storage.events
    assert order.index("put:alpha/_rules.md") < order.index(f"put:{TOKENS_KEY}")
    assert order.index("put:alpha/live/.keep") < order.index(f"put:{TOKENS_KEY}")
    assert order.index("put:alpha/bank/.keep") < order.index(f"put:{TOKENS_KEY}")
    assert order.index(f"put:{TOKENS_KEY}") < order.index("put:alpha/_meta.json")
    assert _stored_tokens(storage).tokens[0].space_ids == ["alpha"]
    audit = json.loads(caplog.records[-1].message)
    assert audit == {
        "event": "space_create",
        "request_id": "-",
        "caller": "manager",
        "actor_token_hash": manager.hash,
        "space_id": "alpha",
        "auto_grant": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_state", ["write_only", "revoked", "expired"])
async def test_space_create_rejects_invalid_persisted_actor_before_prefix_mutation(
    wired, actor_state: str
):
    storage, _ = wired
    actor = _token("manager", "a", permissions=["read", "write", "manage"])
    if actor_state == "write_only":
        actor.permissions = ["read", "write"]
    elif actor_state == "revoked":
        actor.revoked = True
    else:
        actor.expires_at = "2000-01-01T00:00:00+00:00"
    _seed_store(storage, actor)
    before = dict(storage.objects)
    result = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=actor.hash
    )
    assert result["status"] == "error"
    assert storage.objects == before


@pytest.mark.asyncio
async def test_space_create_revalidates_actor_after_waiting_for_token_lock(wired):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["read", "write", "manage"])
    _seed_store(storage, manager)
    locks = locks_module.get_lock_manager()

    await locks.tokens.acquire()
    try:
        pending = asyncio.create_task(
            SpaceService().create(
                "alpha", "description", "# Rules", actor_token_hash=manager.hash
            )
        )
        await asyncio.sleep(0)
        assert not pending.done()
        downgraded = _stored_tokens(storage)
        downgraded.tokens[0].permissions = ["read", "write"]
        storage.objects[TOKENS_KEY] = json.dumps(downgraded.model_dump())
    finally:
        locks.tokens.release()

    result = await pending
    assert result["status"] == "error"
    assert set(storage.objects) == {TOKENS_KEY}


@pytest.mark.asyncio
async def test_space_create_refuses_delete_recreate_with_actor_historical_grant(wired):
    """An absent prefix plus a surviving scope is an ABA recovery case."""
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    _seed_store(storage, manager)
    before = dict(storage.objects)

    result = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert result["status"] == "partial"
    assert result["recovery_required"] is True
    assert result["recovery"]["retry_safe"] is False
    assert "space_ids" in result["recovery"]["action"]
    assert "pré-grant intentionnel" in result["recovery"]["action"]
    assert "space_delete(confirm=True" in result["recovery"]["action"]
    assert "recover_access_grants=True" in result["recovery"]["action"]
    assert storage.objects == before


@pytest.mark.asyncio
async def test_space_delete_revokes_all_grants_then_recreate_grants_only_creator(
    wired, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    storage, _ = wired
    caplog.set_level(logging.INFO, logger="live_mem.audit")
    manager = _token(
        "manager", "a", permissions=["manage"], spaces=["alpha", "beta"]
    )
    reader = _token(
        "reader", "b", permissions=["read"], spaces=["alpha", "beta"]
    )
    revoked = _token(
        "revoked", "c", permissions=["read"], spaces=["alpha"], revoked=True
    )
    expired = _token(
        "expired",
        "d",
        permissions=["read"],
        spaces=["alpha"],
        expires_at="2000-01-01T00:00:00+00:00",
    )
    untouched = _token("untouched", "e", permissions=["read"], spaces=["beta"])
    _seed_store(storage, manager, reader, revoked, expired, untouched)
    _seed_committed_space(storage, "alpha")

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    deleted = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )
    assert deleted["status"] == "deleted"
    assert deleted["files_deleted"] == 4
    assert deleted["files_total"] == 4
    assert deleted["access_grants_removed"] == 4
    delete_events = [event for event in storage.events if event.startswith("delete:")]
    assert delete_events[-1] == "delete:alpha/_meta.json"
    assert set(storage.objects) == {TOKENS_KEY}
    after_delete = {token.name: token for token in _stored_tokens(storage).tokens}
    assert after_delete["manager"].space_ids == ["beta"]
    assert after_delete["reader"].space_ids == ["beta"]
    assert after_delete["revoked"].space_ids == []
    assert after_delete["expired"].space_ids == []
    assert after_delete["untouched"].space_ids == ["beta"]
    grant_audits = [
        json.loads(record.message)
        for record in caplog.records
        if '"event": "space_delete_grants"' in record.message
    ]
    assert grant_audits == [
        {
            "event": "space_delete_grants",
            "request_id": "-",
            "caller": "manager",
            "actor_token_hash": manager.hash,
            "space_id": "alpha",
            "grants_removed": 4,
            "target_token_hashes": [
                manager.hash,
                reader.hash,
                revoked.hash,
                expired.hash,
            ],
            "recovered": False,
        }
    ]

    recreated = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert recreated["status"] == "created"
    after_recreate = {
        token.name: token for token in _stored_tokens(storage).tokens
    }
    assert after_recreate["manager"].space_ids == ["beta", "alpha"]
    assert after_recreate["reader"].space_ids == ["beta"]
    assert after_recreate["revoked"].space_ids == []
    assert after_recreate["expired"].space_ids == []
    assert after_recreate["untouched"].space_ids == ["beta"]


@pytest.mark.asyncio
async def test_space_delete_then_backup_restore_keeps_access_revoked(
    wired, monkeypatch: pytest.MonkeyPatch
):
    storage, token_service = wired
    manager = _token(
        "manager", "a", permissions=["manage"], spaces=["alpha"]
    )
    reader = _token("reader", "b", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, manager, reader)
    _seed_committed_space(storage, "alpha")
    backup_id = "alpha/2026-07-29T20-00-00"
    backup_prefix = f"_backups/{backup_id}/"
    storage.objects[f"{backup_prefix}_meta.json"] = storage.objects[
        "alpha/_meta.json"
    ]
    storage.objects[f"{backup_prefix}_rules.md"] = "# Restored rules"
    storage.objects[f"{backup_prefix}live/restored.md"] = "restored data"

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    deleted = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )
    assert deleted["status"] == "deleted"
    assert deleted["access_grants_removed"] == 2

    restored = await BackupService().restore(backup_id)
    assert restored == {
        "status": "ok",
        "backup_id": backup_id,
        "space_id": "alpha",
        "files_restored": 3,
    }
    assert storage.objects["alpha/live/restored.md"] == "restored data"
    assert all(
        "alpha" not in token.space_ids
        for token in _stored_tokens(storage).tokens
    )

    denied = await token_service.invite_token_to_space(
        actor_token_hash=manager.hash,
        space_id="alpha",
        target_token_hash=reader.hash,
    )
    assert denied == {
        "status": "error",
        "message": "Accès manage actif requis pour cet espace",
    }


@pytest.mark.asyncio
async def test_mcp_delete_restore_and_bootstrap_regrant_flow(
    wired, monkeypatch: pytest.MonkeyPatch
):
    """The public handlers enforce the documented data/access split."""
    from live_mem.tools.admin import register as register_admin
    from live_mem.tools.backup import register as register_backup

    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    reader = _token("reader", "b", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, manager, reader)
    _seed_committed_space(storage, "alpha")
    backup_id = "alpha/2026-07-29T20-00-00"
    backup_prefix = f"_backups/{backup_id}/"
    storage.objects[f"{backup_prefix}_meta.json"] = storage.objects[
        "alpha/_meta.json"
    ]
    storage.objects[f"{backup_prefix}_rules.md"] = "# Restored rules"

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    monkeypatch.setattr(backup_module, "hive_status_label", _local_only)
    deleted = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )
    assert deleted["status"] == "deleted"

    restore = _handler(register_backup, "backup_restore")
    manager_identity = {
        "type": "token",
        "client_name": manager.name,
        "permissions": manager.permissions,
        "allowed_resources": ["alpha"],
        "token_hash": manager.hash,
    }
    manager_context = auth_context.current_token_info.set(manager_identity)
    try:
        denied = await restore(backup_id, confirm=True)
    finally:
        auth_context.current_token_info.reset(manager_context)
    assert denied["status"] == "error"
    assert "Authentification" in denied["message"]

    bootstrap_identity = {
        "type": "bootstrap",
        "client_name": "bootstrap_admin",
        "permissions": ["admin"],
        "allowed_resources": [],
    }
    bootstrap_context = auth_context.current_token_info.set(bootstrap_identity)
    try:
        restored = await restore(backup_id, confirm=True)
        regranted = await _handler(register_admin, "admin_update_token")(
            reader.hash,
            space_ids_add="alpha",
        )
    finally:
        auth_context.current_token_info.reset(bootstrap_context)

    assert restored == {
        "status": "ok",
        "backup_id": backup_id,
        "space_id": "alpha",
        "files_restored": 2,
    }
    assert regranted["status"] == "ok"
    persisted = {token.name: token for token in _stored_tokens(storage).tokens}
    assert persisted["manager"].space_ids == []
    assert persisted["reader"].space_ids == ["alpha"]


@pytest.mark.asyncio
async def test_space_delete_recovery_flag_on_committed_space_still_reports_deleted(
    wired, monkeypatch: pytest.MonkeyPatch
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    _seed_store(storage, manager)
    _seed_committed_space(storage, "alpha")

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    result = await SpaceService().delete(
        "alpha",
        recover_access_grants=True,
        actor_token_hash=manager.hash,
    )

    assert result == {
        "status": "deleted",
        "space_id": "alpha",
        "files_deleted": 4,
        "files_total": 4,
        "access_grants_removed": 1,
    }
    assert set(storage.objects) == {TOKENS_KEY}


@pytest.mark.asyncio
async def test_space_delete_payload_failure_preserves_marker_and_reports_exact_key(
    wired, monkeypatch: pytest.MonkeyPatch
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    _seed_store(storage, manager)
    _seed_committed_space(storage, "alpha")
    failing_key = "alpha/bank/.keep"
    storage.fail_before_delete.add(failing_key)
    tokens_before = storage.objects[TOKENS_KEY]

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    result = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )

    assert result["status"] == "partial"
    assert result["recovery_required"] is True
    assert result["failed_keys"] == [failing_key]
    assert result["marker_preserved"] is True
    assert result["files_total"] == 4
    assert result["files_deleted"] == 2
    assert "alpha/_meta.json" in storage.objects
    assert "delete:alpha/_meta.json" not in storage.events
    assert storage.objects[TOKENS_KEY] == tokens_before


@pytest.mark.asyncio
@pytest.mark.parametrize("timed_out_key", ["alpha/bank/.keep", "alpha/_meta.json"])
async def test_space_delete_post_delete_timeout_is_success_when_absence_is_confirmed(
    wired, monkeypatch: pytest.MonkeyPatch, timed_out_key: str
):
    storage, _ = wired
    _seed_store(storage, _token("admin", "a", permissions=["admin"]))
    _seed_committed_space(storage, "alpha")
    storage.delete_then_raise.add(timed_out_key)
    # No token scope changes, so the validated authorization read under the
    # token lock is already sufficient and a redundant confirmation GET must
    # not downgrade an otherwise complete deletion.
    storage.fail_get_calls[TOKENS_KEY] = {2}

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    result = await SpaceService().delete(
        "alpha", actor_token_hash=_hash("a")
    )

    assert result == {
        "status": "deleted",
        "space_id": "alpha",
        "files_deleted": 4,
        "files_total": 4,
        "access_grants_removed": 0,
    }
    assert storage.get_counts[TOKENS_KEY] == 1
    assert f"put:{TOKENS_KEY}" not in storage.events
    assert set(storage.objects) == {TOKENS_KEY}


@pytest.mark.asyncio
@pytest.mark.parametrize("ambiguous_reprobe", [False, True])
async def test_space_delete_marker_failure_is_never_reported_deleted(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    ambiguous_reprobe: bool,
):
    storage, _ = wired
    _seed_store(storage, _token("admin", "a", permissions=["admin"]))
    _seed_committed_space(storage, "alpha")
    meta_key = "alpha/_meta.json"
    storage.fail_before_delete.add(meta_key)
    tokens_before = storage.objects[TOKENS_KEY]
    if ambiguous_reprobe:
        # Initial existence check is call 1; the post-DELETE confirmation is 2.
        storage.fail_exists_calls[meta_key] = {2}

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    result = await SpaceService().delete(
        "alpha", actor_token_hash=_hash("a")
    )

    assert result["status"] == "partial"
    assert result["failed_keys"] == [meta_key]
    assert result["files_deleted"] == 3
    assert result["marker_preserved"] is (None if ambiguous_reprobe else True)
    assert meta_key in storage.objects
    assert storage.objects[TOKENS_KEY] == tokens_before


@pytest.mark.asyncio
async def test_space_delete_token_save_failure_requires_explicit_empty_prefix_recovery(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    reader = _token("reader", "b", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, manager, reader)
    _seed_committed_space(storage, "alpha")
    storage.fail_before_put.add(TOKENS_KEY)

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        first = await SpaceService().delete(
            "alpha", actor_token_hash=manager.hash
        )

    assert first["status"] == "partial"
    assert first["recovery_required"] is True
    assert first["marker_preserved"] is False
    assert first["failed_keys"] == [TOKENS_KEY]
    assert first["access_grants_pending"] == 2
    assert first["recovery"]["retry_safe"] is True
    assert "recover_access_grants=True" in first["recovery"]["action"]
    assert not any(key.startswith("alpha/") for key in storage.objects)
    assert [token.space_ids for token in _stored_tokens(storage).tokens] == [
        ["alpha"],
        ["alpha"],
    ]
    assert not [
        record
        for record in caplog.records
        if '"event": "space_delete_grants"' in record.message
    ]

    storage.fail_before_put.clear()
    second = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )
    assert second["status"] == "not_found"
    assert "recover_access_grants=True" in second["message"]
    assert [token.space_ids for token in _stored_tokens(storage).tokens] == [
        ["alpha"],
        ["alpha"],
    ]

    recovered = await SpaceService().delete(
        "alpha",
        recover_access_grants=True,
        actor_token_hash=manager.hash,
    )
    assert recovered == {
        "status": "grants_cleaned",
        "space_id": "alpha",
        "files_deleted": 0,
        "files_total": 0,
        "access_grants_removed": 2,
        "recovered": True,
    }
    assert all(
        "alpha" not in token.space_ids
        for token in _stored_tokens(storage).tokens
    )

    manager_retry = await SpaceService().delete(
        "alpha",
        recover_access_grants=True,
        actor_token_hash=manager.hash,
    )
    assert manager_retry["status"] == "error"
    assert "manage" in manager_retry["message"]

    bootstrap_retry = await SpaceService().delete(
        "alpha",
        recover_access_grants=True,
        bootstrap_admin=True,
    )
    assert bootstrap_retry == {
        "status": "not_found",
        "space_id": "alpha",
        "message": "Espace 'alpha' introuvable",
    }


@pytest.mark.asyncio
async def test_space_delete_absent_future_pregrant_is_not_destructive_by_default(
    wired,
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    reader = _token("reader", "b", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, manager, reader)
    tokens_before = storage.objects[TOKENS_KEY]

    result = await SpaceService().delete(
        "alpha",
        actor_token_hash=manager.hash,
    )

    assert result["status"] == "not_found"
    assert result["space_id"] == "alpha"
    assert "pré-grants intentionnels" in result["message"]
    assert "recover_access_grants=True" in result["message"]
    assert storage.objects[TOKENS_KEY] == tokens_before
    assert f"put:{TOKENS_KEY}" not in storage.events


@pytest.mark.asyncio
async def test_space_delete_absent_without_grants_returns_identified_not_found(
    wired,
):
    storage, _ = wired
    _seed_store(storage)

    result = await SpaceService().delete(
        "alpha",
        bootstrap_admin=True,
    )

    assert result == {
        "status": "not_found",
        "space_id": "alpha",
        "message": "Espace 'alpha' introuvable",
    }


@pytest.mark.asyncio
async def test_space_delete_token_post_put_timeout_is_success_only_after_clean_reprobe(
    wired, monkeypatch: pytest.MonkeyPatch
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    _seed_store(storage, manager)
    _seed_committed_space(storage, "alpha")
    storage.persist_then_raise.add(TOKENS_KEY)

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    result = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )

    assert result["status"] == "deleted"
    assert result["access_grants_removed"] == 1
    assert _stored_tokens(storage).tokens[0].space_ids == []


@pytest.mark.asyncio
async def test_space_delete_bootstrap_admin_revokes_grants_without_stored_actor(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    storage, _ = wired
    caplog.set_level(logging.INFO, logger="live_mem.audit")
    reader = _token("reader", "b", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, reader)
    _seed_committed_space(storage, "alpha")

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    result = await SpaceService().delete(
        "alpha",
        actor_token_hash="",
        bootstrap_admin=True,
    )

    assert result["status"] == "deleted"
    assert result["access_grants_removed"] == 1
    assert _stored_tokens(storage).tokens[0].space_ids == []
    grant_audits = [
        json.loads(record.message)
        for record in caplog.records
        if '"event": "space_delete_grants"' in record.message
    ]
    assert grant_audits == [
        {
            "event": "space_delete_grants",
            "request_id": "-",
            "caller": "bootstrap_admin",
            "actor_token_hash": None,
            "space_id": "alpha",
            "grants_removed": 1,
            "target_token_hashes": [reader.hash],
            "recovered": False,
        }
    ]


@pytest.mark.asyncio
async def test_space_delete_bootstrap_admin_can_retry_pending_grant_cleanup(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    storage, _ = wired
    reader = _token("reader", "b", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, reader)
    _seed_committed_space(storage, "alpha")
    storage.fail_before_put.add(TOKENS_KEY)

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        result = await SpaceService().delete(
            "alpha",
            bootstrap_admin=True,
        )

    assert result["status"] == "partial"
    assert result["access_grants_pending"] == 1
    assert result["recovery"]["retry_safe"] is True
    assert "recover_access_grants=True" in result["recovery"]["action"]
    assert not [
        record
        for record in caplog.records
        if '"event": "space_delete_grants"' in record.message
    ]


@pytest.mark.asyncio
async def test_space_delete_locked_rejects_missing_actor_before_mutation(wired):
    storage, _ = wired
    _seed_store(storage)
    _seed_committed_space(storage, "alpha")
    before = dict(storage.objects)

    result = await SpaceService()._delete_locked(
        "alpha",
        token_service=None,
        token_store=None,
        actor=None,
        bootstrap_admin=False,
    )

    assert result == {
        "status": "error",
        "message": "Identité stockée du manager requise avant toute suppression",
    }
    assert storage.objects == before
    assert storage.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("invalid_json", json.JSONDecodeError),
        ("future_version", RuntimeError),
        ("read_timeout", TimeoutError),
    ],
)
async def test_space_delete_invalid_token_registry_fails_before_prefix_mutation(
    wired,
    failure: str,
    expected_error: type[Exception],
):
    storage, _ = wired
    _seed_committed_space(storage, "alpha")
    if failure == "invalid_json":
        storage.objects[TOKENS_KEY] = "{invalid"
    elif failure == "future_version":
        storage.objects[TOKENS_KEY] = json.dumps({"version": 3, "tokens": []})
    else:
        _seed_store(storage)
        storage.fail_get_calls[TOKENS_KEY] = {1}
    before = dict(storage.objects)

    with pytest.raises(expected_error):
        await SpaceService().delete(
            "alpha",
            actor_token_hash="",
            bootstrap_admin=True,
        )

    assert storage.objects == before
    assert not [
        event
        for event in storage.events
        if event.startswith("delete:") or event == "delete_many"
    ]


@pytest.mark.asyncio
async def test_space_delete_token_confirmation_read_failure_never_reports_success(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    _seed_store(storage, manager)
    _seed_committed_space(storage, "alpha")
    auth_context.update_fresh_token(
        {
            "type": "token",
            "client_name": manager.name,
            "permissions": list(manager.permissions),
            "allowed_resources": list(manager.space_ids),
            "token_hash": manager.hash,
        }
    )
    # Call 1 authorizes the actor; call 2 confirms the token cleanup.
    storage.fail_get_calls[TOKENS_KEY] = {2}

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        result = await SpaceService().delete(
            "alpha", actor_token_hash=manager.hash
        )

    assert result["status"] == "partial"
    assert result["marker_preserved"] is False
    assert result["failed_keys"] == [TOKENS_KEY]
    assert result["access_grants_pending"] is None
    assert result["recovery"]["retry_safe"] is None
    assert not any(key.startswith("alpha/") for key in storage.objects)
    # The write did persist, but unreadable authority can never be reported as
    # successful.
    assert _stored_tokens(storage).tokens[0].space_ids == []
    assert manager.hash in auth_context._invalidated_token_hashes
    assert manager.hash not in auth_context._fresh_token_store
    assert not [
        record
        for record in caplog.records
        if '"event": "space_delete_grants"' in record.message
    ]
    unconfirmed = [
        json.loads(record.message)
        for record in caplog.records
        if '"event": "space_delete_grants_unconfirmed"' in record.message
    ]
    assert unconfirmed == [
        {
            "event": "space_delete_grants_unconfirmed",
            "request_id": "-",
            "caller": "manager",
            "actor_token_hash": manager.hash,
            "space_id": "alpha",
            "target_token_hashes": [manager.hash],
            "recovered": False,
            "confirmation": "unreadable",
        }
    ]


@pytest.mark.asyncio
async def test_space_delete_invalidates_every_changed_fresh_token(
    wired, monkeypatch: pytest.MonkeyPatch
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    reader = _token("reader", "b", permissions=["read"], spaces=["alpha"])
    untouched = _token("untouched", "c", permissions=["read"], spaces=["beta"])
    _seed_store(storage, manager, reader, untouched)
    _seed_committed_space(storage, "alpha")
    for token in (manager, reader, untouched):
        auth_context.update_fresh_token(
            {
                "type": "token",
                "client_name": token.name,
                "permissions": list(token.permissions),
                "allowed_resources": list(token.space_ids),
                "token_hash": token.hash,
            }
        )

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    result = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )

    assert result["status"] == "deleted"
    assert auth_context._invalidated_token_hashes == {
        manager.hash,
        reader.hash,
    }
    assert set(auth_context._fresh_token_store) == {untouched.hash}


@pytest.mark.asyncio
async def test_space_delete_confirmation_regrant_after_actor_revocation_is_not_retryable(
    wired,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    storage, token_service = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    reader = _token("reader", "b", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, manager, reader)
    _seed_committed_space(storage, "alpha")

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    original_load = token_service._load_store
    load_count = 0

    async def _load_with_concurrent_regrant():
        nonlocal load_count
        load_count += 1
        if load_count == 2:
            revoked_manager = manager.model_copy(deep=True)
            revoked_manager.revoked = True
            storage.objects[TOKENS_KEY] = json.dumps(
                TokensStore(tokens=[revoked_manager, reader]).model_dump()
            )
        return await original_load()

    monkeypatch.setattr(token_service, "_load_store", _load_with_concurrent_regrant)
    with caplog.at_level(logging.INFO, logger="live_mem.audit"):
        result = await SpaceService().delete(
            "alpha",
            actor_token_hash=manager.hash,
        )

    assert result["status"] == "partial"
    assert result["marker_preserved"] is False
    assert result["access_grants_pending"] == 2
    assert result["recovery"]["retry_safe"] is False
    assert "caller ne possède plus" in result["recovery"]["action"]
    assert "Un admin doit retenter" in result["recovery"]["action"]
    assert not any(key.startswith("alpha/") for key in storage.objects)
    assert not [
        record
        for record in caplog.records
        if '"event": "space_delete_grants"' in record.message
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["downgrade", "rescope", "revoke", "expire"])
async def test_space_delete_revalidates_actor_after_waiting_for_token_lock(
    wired, monkeypatch: pytest.MonkeyPatch, change: str
):
    storage, _ = wired
    manager = _token(
        "manager", "a", permissions=["read", "write", "manage"], spaces=["alpha"]
    )
    _seed_store(storage, manager)
    _seed_committed_space(storage, "alpha")
    before_prefix = {
        key: value
        for key, value in storage.objects.items()
        if key.startswith("alpha/")
    }
    locks = locks_module.get_lock_manager()

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    await locks.tokens.acquire()
    try:
        pending = asyncio.create_task(
            SpaceService().delete(
                "alpha", actor_token_hash=manager.hash
            )
        )
        await asyncio.sleep(0)
        assert not pending.done()
        changed = _stored_tokens(storage)
        if change == "downgrade":
            changed.tokens[0].permissions = ["read", "write"]
        elif change == "rescope":
            changed.tokens[0].space_ids = []
        elif change == "revoke":
            changed.tokens[0].revoked = True
        else:
            changed.tokens[0].expires_at = "2000-01-01T00:00:00+00:00"
        storage.objects[TOKENS_KEY] = json.dumps(changed.model_dump())
    finally:
        locks.tokens.release()

    result = await pending
    assert result["status"] == "error"
    assert "manage" in result["message"]
    assert {
        key: value
        for key, value in storage.objects.items()
        if key.startswith("alpha/")
    } == before_prefix


@pytest.mark.asyncio
async def test_space_delete_late_preparation_keeps_marker_then_retry_blocks_aba(
    wired, monkeypatch: pytest.MonkeyPatch
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    _seed_store(storage, manager)
    _seed_committed_space(storage, "alpha")
    # Simule une écriture non sérialisée qui recrée le dernier sentinel
    # après son DELETE mais avant la re-LIST finale.
    last_payload = "alpha/live/.keep"
    storage.inject_after_delete[last_payload] = (last_payload, "")

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    first = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )
    assert first["status"] == "partial"
    assert first["failed_keys"] == [last_payload]
    assert first["marker_preserved"] is True
    assert "alpha/_meta.json" in storage.objects

    storage.inject_after_delete.clear()
    second = await SpaceService().delete(
        "alpha", actor_token_hash=manager.hash
    )
    assert second["status"] == "deleted"
    assert second["access_grants_removed"] == 1
    assert set(storage.objects) == {TOKENS_KEY}

    recreated = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert recreated["status"] == "created"
    assert _stored_tokens(storage).tokens[0].space_ids == ["alpha"]


@pytest.mark.asyncio
async def test_space_delete_marker_absent_with_residual_refuses_automatic_cleanup(
    wired,
):
    storage, _ = wired
    _seed_store(storage, _token("admin", "a", permissions=["admin"]))
    storage.objects["alpha/live/late.md"] = "late"
    before = dict(storage.objects)

    result = await SpaceService().delete(
        "alpha", actor_token_hash=_hash("a")
    )

    assert result["status"] == "partial"
    assert result["marker_preserved"] is False
    assert result["failed_keys"] == ["alpha/live/late.md"]
    assert result["recovery"]["retry_safe"] is False
    assert storage.objects == before


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_kind", ["revoked", "expired"])
async def test_space_create_bootstrap_refuses_stale_inactive_grant_without_mutation(
    wired, stale_kind: str
):
    storage, _ = wired
    stale = _token("stale", "b", permissions=["read"], spaces=["alpha"])
    if stale_kind == "revoked":
        stale.revoked = True
    else:
        stale.expires_at = "2000-01-01T00:00:00+00:00"
    _seed_store(storage, stale)
    before = dict(storage.objects)

    result = await SpaceService().create(
        "alpha", "description", "# Rules", bootstrap_admin=True
    )
    assert result["status"] == "partial"
    assert storage.objects == before


@pytest.mark.asyncio
async def test_space_create_barrier_counts_dormant_admin_scope_defense_in_depth(
    wired, monkeypatch: pytest.MonkeyPatch
):
    """Even a pre-migration admin scope cannot bypass the ABA barrier."""
    storage, service = wired
    dormant_admin = _token(
        "legacy-admin", "a", permissions=["admin"], spaces=["alpha"]
    )
    legacy_loaded = TokensStore(version=1, tokens=[dormant_admin])

    async def _load_pre_migration_object():
        return legacy_loaded

    monkeypatch.setattr(service, "_load_store", _load_pre_migration_object)
    before = dict(storage.objects)
    result = await SpaceService().create(
        "alpha", "description", "# Rules", bootstrap_admin=True
    )

    assert result["status"] == "partial"
    assert result["recovery_required"] is True
    assert "tous les tokens" in result["recovery"]["action"]
    assert storage.objects == before


@pytest.mark.asyncio
async def test_space_create_actor_grant_requires_cleanup_before_compatible_resume(
    wired,
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    _seed_store(storage, manager)
    storage.objects["alpha/_rules.md"] = "# Rules"
    before = dict(storage.objects)

    denied = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert denied["status"] == "partial"
    assert denied["recovery"]["retry_safe"] is False
    assert "tous les tokens" in denied["recovery"]["action"]
    assert storage.objects == before

    cleaned = _stored_tokens(storage)
    cleaned.tokens[0].space_ids = []
    storage.objects[TOKENS_KEY] = json.dumps(cleaned.model_dump())
    resumed = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert resumed["status"] == "created"
    assert "alpha/_meta.json" in storage.objects
    assert resumed["token_auto_updated"] is True
    assert _stored_tokens(storage).tokens[0].space_ids == ["alpha"]


@pytest.mark.asyncio
async def test_space_create_refuses_compatible_prefix_with_foreign_grant(wired):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"])
    foreign = _token("foreign", "b", permissions=["read"], spaces=["alpha"])
    _seed_store(storage, manager, foreign)
    storage.objects["alpha/_rules.md"] = "# Rules"
    before = dict(storage.objects)

    result = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert result["status"] == "partial"
    assert "référence de scope" in result["message"]
    assert storage.objects == before


@pytest.mark.asyncio
async def test_space_create_succeeds_after_explicit_stale_scope_cleanup(wired):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"], spaces=["alpha"])
    _seed_store(storage, manager)

    denied = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert denied["status"] == "partial"

    cleaned = _stored_tokens(storage)
    cleaned.tokens[0].space_ids = []
    storage.objects[TOKENS_KEY] = json.dumps(cleaned.model_dump())
    created = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert created["status"] == "created"
    assert _stored_tokens(storage).tokens[0].space_ids == ["alpha"]


@pytest.mark.asyncio
async def test_committed_space_remains_already_exists_despite_historical_grant(wired):
    storage, _ = wired
    stale = _token(
        "stale", "b", permissions=["read"], spaces=["alpha"], revoked=True
    )
    _seed_store(storage, stale)
    _seed_committed_space(storage, "alpha")
    before = dict(storage.objects)

    result = await SpaceService().create(
        "alpha", "description", "# Rules", bootstrap_admin=True
    )
    assert result["status"] == "already_exists"
    assert storage.objects == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_key",
    ["alpha/_rules.md", "alpha/live/.keep", "alpha/bank/.keep"],
)
async def test_each_space_prepare_put_failure_is_retryable_without_meta_or_grant(
    wired, failing_key: str
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"])
    _seed_store(storage, manager)
    storage.fail_before_put.add(failing_key)

    result = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert result["status"] == "partial"
    assert result["recovery"]["retry_safe"] is True
    assert "alpha/_meta.json" not in storage.objects
    assert _stored_tokens(storage).tokens[0].space_ids == []
    # Never rollback a successfully prepared predecessor.
    expected_predecessors = [
        "alpha/_rules.md",
        "alpha/live/.keep",
        "alpha/bank/.keep",
    ]
    failure_index = expected_predecessors.index(failing_key)
    assert all(key in storage.objects for key in expected_predecessors[:failure_index])


@pytest.mark.asyncio
async def test_space_grant_save_failure_keeps_prepared_prefix_and_no_meta(wired):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"])
    _seed_store(storage, manager)
    storage.fail_before_put.add(TOKENS_KEY)

    result = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert result["status"] == "partial"
    assert result["recovery"]["retry_safe"] is True
    assert "alpha/_meta.json" not in storage.objects
    assert _stored_tokens(storage).tokens[0].space_ids == []
    assert {
        "alpha/_rules.md",
        "alpha/live/.keep",
        "alpha/bank/.keep",
    }.issubset(storage.objects)


@pytest.mark.asyncio
async def test_ambiguous_persisted_space_grant_is_not_directly_retryable(wired):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"])
    _seed_store(storage, manager)
    storage.persist_then_raise.add(TOKENS_KEY)
    # Call 1 authorizes the actor; call 2 is the post-timeout grant reprobe.
    storage.fail_get_calls[TOKENS_KEY] = {2}

    result = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )

    assert result["status"] == "partial"
    assert result["recovery"]["retry_safe"] is False
    assert "Un admin doit d'abord inspecter" in result["recovery"]["action"]
    assert "alpha/_meta.json" not in storage.objects
    # The timeout happened after persistence, so the durable grant exists even
    # though its immediate reprobe was unreadable.
    assert _stored_tokens(storage).tokens[0].space_ids == ["alpha"]


@pytest.mark.asyncio
async def test_space_create_resumes_only_matching_prefix_and_never_rolls_back(wired):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"])
    _seed_store(storage, manager)
    storage.objects["alpha/_rules.md"] = "# Rules\n"

    result = await SpaceService().create(
        "alpha", "description", "# Rules\n", actor_token_hash=manager.hash
    )
    assert result["status"] == "created"
    assert "alpha/live/.keep" in storage.objects
    assert "alpha/bank/.keep" in storage.objects

    # A different request over an uncommitted prefix is a hard recovery path.
    storage.objects.pop("alpha/_meta.json")
    before = dict(storage.objects)
    conflict = await SpaceService().create(
        "alpha", "description", "# Different\n", actor_token_hash=manager.hash
    )
    assert conflict["status"] == "partial"
    assert conflict["recovery"]["retry_safe"] is False
    assert storage.objects == before


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["corrupt_meta", "incomplete_committed"])
async def test_meta_present_requires_valid_marker_and_constitutive_objects(wired, kind: str):
    storage, _ = wired
    admin = _token("admin", "a", permissions=["admin"])
    _seed_store(storage, admin)
    if kind == "corrupt_meta":
        storage.objects["alpha/_meta.json"] = "{not-json"
    else:
        storage.objects["alpha/_meta.json"] = json.dumps(
            {"space_id": "alpha", "description": "x", "created_at": "now"}
        )
        storage.objects["alpha/_rules.md"] = "# Rules"
    before = dict(storage.objects)

    result = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=admin.hash
    )
    assert result["status"] == "partial"
    assert result["recovery_required"] is True
    assert result["recovery"]["retry_safe"] is False
    assert storage.objects == before


@pytest.mark.asyncio
async def test_ambiguous_meta_put_is_created_only_when_reprobe_confirms(wired):
    storage, _ = wired
    admin = _token("admin", "a", permissions=["admin"])
    _seed_store(storage, admin)
    storage.persist_then_raise.add("alpha/_meta.json")
    confirmed = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=admin.hash
    )
    assert confirmed["status"] == "created"

    # A pre-PUT failure leaves a compatible prepared prefix and an explicit retry.
    storage.objects = {TOKENS_KEY: storage.objects[TOKENS_KEY]}
    storage.events.clear()
    storage.persist_then_raise.clear()
    storage.fail_before_put.add("beta/_meta.json")
    partial = await SpaceService().create(
        "beta", "description", "# Rules", actor_token_hash=admin.hash
    )
    assert partial["status"] == "partial"
    assert partial["recovery"]["retry_safe"] is True
    assert "beta/_meta.json" not in storage.objects


@pytest.mark.asyncio
async def test_post_grant_marker_failure_requires_cleanup_before_identical_retry(
    wired,
):
    storage, _ = wired
    manager = _token("manager", "a", permissions=["manage"])
    _seed_store(storage, manager)
    storage.fail_before_put.add("alpha/_meta.json")

    first = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert first["status"] == "partial"
    assert first["recovery"]["retry_safe"] is False
    assert "Aucun rollback automatique" in first["recovery"]["action"]
    assert "alpha/_meta.json" not in storage.objects
    assert _stored_tokens(storage).tokens[0].space_ids == ["alpha"]
    prepared = {
        key: value
        for key, value in storage.objects.items()
        if key.startswith("alpha/")
    }

    storage.fail_before_put.clear()
    refused_retry = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert refused_retry["status"] == "partial"
    assert refused_retry["recovery"]["retry_safe"] is False
    assert {
        key: value
        for key, value in storage.objects.items()
        if key.startswith("alpha/")
    } == prepared

    cleaned = _stored_tokens(storage)
    cleaned.tokens[0].space_ids = []
    storage.objects[TOKENS_KEY] = json.dumps(cleaned.model_dump())
    resumed = await SpaceService().create(
        "alpha", "description", "# Rules", actor_token_hash=manager.hash
    )
    assert resumed["status"] == "created"
    assert resumed["token_auto_updated"] is True
    assert _stored_tokens(storage).tokens[0].space_ids == ["alpha"]


@pytest.mark.asyncio
async def test_create_and_delete_share_space_lifecycle_lock(
    wired, monkeypatch: pytest.MonkeyPatch
):
    storage, _ = wired
    admin = _token("admin", "a", permissions=["admin"])
    _seed_store(storage, admin)
    storage.pause_put_key = "alpha/_rules.md"
    storage.put_started = asyncio.Event()
    storage.release_put = asyncio.Event()

    async def _local_only(*_args, **_kwargs):
        return "local_only"

    monkeypatch.setattr(space_module, "hive_status_label", _local_only)
    creator = asyncio.create_task(
        SpaceService().create(
            "alpha", "description", "# Rules", actor_token_hash=admin.hash
        )
    )
    await storage.put_started.wait()
    deleter = asyncio.create_task(
        SpaceService().delete("alpha", actor_token_hash=admin.hash)
    )
    await asyncio.sleep(0)
    assert not deleter.done(), "delete must wait for the in-flight create commit"
    storage.release_put.set()
    assert (await creator)["status"] == "created"
    assert (await deleter)["status"] == "deleted"


def _function_body(module, name: str) -> str:
    source = inspect.getsource(module.register)
    match = re.search(
        rf"async def {name}\(.*?(?=\n    @mcp\.tool|\n    return \d+|\Z)",
        source,
        re.DOTALL,
    )
    assert match, f"handler {name} not found"
    return match.group(0)


def test_space_create_mutation_guard_manage_only_no_actorless_grant():
    from live_mem.tools import space

    body = _function_body(space, "space_create")
    assert "check_manage_permission" in body
    assert "check_write_permission" not in body
    assert "add_space_to_token" not in inspect.getsource(tokens_module.TokenService)
    core = inspect.getsource(SpaceService._create_locked)
    barrier = core[core.index("scoped_tokens =") : core.index("post_grant_recovery")]
    assert "if space_id in token.space_ids" in barrier
    assert '"admin" not in' not in barrier
    assert core.index("await token_service._save_store") < core.index(
        "await storage.put_json(meta_key, meta)"
    )
    delete = inspect.getsource(SpaceService.delete)
    assert delete.index("locks.space_lifecycle") < delete.index("locks.tokens")
    assert delete.index("locks.tokens") < delete.index("self._delete_locked")
    delete_locked = inspect.getsource(SpaceService._delete_locked)
    assert delete_locked.index("marker_absent = await delete_and_confirm") < (
        delete_locked.rindex("return await finish_access_cleanup")
    )


@pytest.mark.parametrize(
    ("module_name", "handler", "durable_marker"),
    [
        ("live", "live_note", "return await engine.write_note"),
        ("space", "space_update", "return await get_space_service().update"),
        ("backup", "backup_create", "return await get_backup_service().create(space_id"),
        ("graph", "graph_connect", "return await get_engine_registry().long_engine().connect"),
        ("graph", "graph_push", "return await get_engine_registry().long_engine().push"),
        ("graph", "graph_disconnect", "return await get_engine_registry().long_engine().disconnect"),
        ("bank", "bank_consolidate", "await get_consolidation_queue().enqueue"),
    ],
)
def test_write_space_mutations_keep_allowlist_guard_before_durable_path(
    module_name: str, handler: str, durable_marker: str
):
    """Removing check_access while retaining write permission must be RED."""
    module = __import__(f"live_mem.tools.{module_name}", fromlist=["register"])
    body = _function_body(module, handler)
    guard = "access_err = check_access(space_id)"
    assert guard in body
    assert durable_marker in body
    assert body.index(guard) < body.index(durable_marker)
