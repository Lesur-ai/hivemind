# -*- coding: utf-8 -*-
"""
P7-4 (#120) — Unified token authority (Model B) RED→GREEN spec.

Hivemind is the single token authority: the embedded Graph Memory validates the
SAME tokens as Hivemind by reading `_system/tokens.json` from the shared S3
bucket — there is NO separate GM token store on the live auth path. This module
locks the nine resolved gate-review NO-GOs as executable tests.

Test strategy (the Hivemind test venv has boto3 but NOT neo4j/qdrant):
- The validator (`mcp_memory.auth.s3_token_validator`) is import-light and takes
  an injected `read_tokens_json` async reader + `clock`, so its behaviour is
  unit-tested with NO real S3 / Neo4j (NO-GO #5,#6,#8,#9, revoked/expired).
- `mcp_memory.auth.context` is import-light → imported directly to lock the
  fail-closed `auth is None → deny` flip (NO-GO #4a).
- `mcp_memory.auth.middleware` pulls neo4j → asserted via SOURCE inspection only
  (NO-GO #4b bypass default / #4c CORS, #7 Neo4j REPLACE, #9 contextvar reset).

These tests are RED until P7-4 lands `s3_token_validator.py`, flips the context
checks, and patches the middleware.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GM = _REPO_ROOT / "services" / "graph-memory" / "src" / "mcp_memory"
_MIDDLEWARE_SRC = (_GM / "auth" / "middleware.py")


# --------------------------------------------------------------------------- #
# Helpers — fake Hivemind token store + injected reader/clock                  #
# --------------------------------------------------------------------------- #

def _hivemind_hash(raw_token: str) -> str:
    """Mirror Hivemind tokens.py: stored hash is 'sha256:'+hexdigest."""
    return "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()


def _store(*tokens: dict) -> str:
    """Serialize a Hivemind TokensStore JSON ({version, tokens:[...]})."""
    return json.dumps({"version": 2, "tokens": list(tokens)})


def _token_entry(
    raw_token: str,
    *,
    name: str = "internal-long",
    permissions=None,
    space_ids=None,
    revoked: bool = False,
    expires_at: str | None = None,
    hash_override: str | None = None,
) -> dict:
    return {
        "hash": hash_override if hash_override is not None else _hivemind_hash(raw_token),
        "name": name,
        "email": "",
        "permissions": list(permissions if permissions is not None else ["read", "write"]),
        "space_ids": list(space_ids if space_ids is not None else []),
        "created_at": "2026-01-01T00:00:00+00:00",
        "expires_at": expires_at,
        "last_used_at": None,
        "revoked": revoked,
    }


class _Reader:
    """Injected async tokens.json reader that counts reads (to prove caching)."""

    def __init__(self, payload: str | None):
        self.payload = payload
        self.calls = 0

    async def __call__(self) -> str | None:
        self.calls += 1
        return self.payload


def _make_validator(reader, *, signature_mode=None, clock=None, cache_ttl_seconds=30):
    from mcp_memory.auth.s3_token_validator import S3TokenValidator  # RED until created

    return S3TokenValidator(
        read_tokens_json=reader,
        signature_mode=signature_mode,
        clock=clock,
        cache_ttl_seconds=cache_ttl_seconds,
    )


def _validate(validator, raw_token):
    return asyncio.run(validator.validate_token(raw_token))


# --------------------------------------------------------------------------- #
# NO-GO #6 — reads Hivemind schema (space_ids), sha256: prefix, manage→write   #
# --------------------------------------------------------------------------- #

def test_validates_hivemind_token_with_sha256_prefix():
    raw = "lm_secret_one"
    reader = _Reader(_store(_token_entry(raw, name="agent-x", permissions=["read", "write"])))
    info = _validate(_make_validator(reader), raw)
    assert info is not None, "a live, non-revoked Hivemind token must validate"
    assert info.client_name == "agent-x"
    assert "write" in info.permissions


def test_manage_permission_projects_to_write():
    # GM check_write_permission accepts only read/write/admin — a Hivemind
    # `manage` token would lose long-tier write without projection.
    raw = "lm_manage_token"
    reader = _Reader(_store(_token_entry(raw, permissions=["read", "manage"])))
    info = _validate(_make_validator(reader), raw)
    assert info is not None
    assert "write" in info.permissions, "Hivemind 'manage' must project to GM 'write'"


def test_ignores_allowed_resources_key_and_uses_space_ids():
    # Hivemind's persisted schema uses `space_ids`; there is NO `allowed_resources`
    # key. The validator must not depend on `allowed_resources` existing.
    raw = "lm_space_scoped"
    entry = _token_entry(raw, permissions=["read", "write"], space_ids=["alpha"])
    assert "allowed_resources" not in entry
    reader = _Reader(_store(entry))
    info = _validate(_make_validator(reader), raw)
    assert info is not None


def test_bare_hex_hash_in_store_is_rejected():
    # The derived authority must not reinterpret a corrupt non-canonical hash.
    # Hivemind's critical store requires the full sha256:<64 lowercase hex> form.
    raw = "lm_bare_hex"
    bare = hashlib.sha256(raw.encode()).hexdigest()  # no 'sha256:' prefix
    reader = _Reader(_store(_token_entry(raw, hash_override=bare)))
    info = _validate(_make_validator(reader), raw)
    assert info is None, "stored bare-hex hash must fail closed"


@pytest.mark.parametrize(
    "case",
    [
        "entry_not_object",
        "bare_hash",
        "duplicate_hash",
        "unknown_permission",
        "duplicate_permission",
        "invalid_space",
        "duplicate_space",
        "admin_scope",
        "revoked_not_bool",
        "expires_not_string",
        "name_not_string",
    ],
)
def test_corrupt_non_target_entry_invalidates_entire_store(case):
    """GM must agree with Hivemind's whole-registry fail-closed boundary."""
    raw = "lm_valid_target"
    target = _token_entry(raw)
    corrupt: object = _token_entry("lm_other")
    assert isinstance(corrupt, dict)
    if case == "entry_not_object":
        corrupt = "not-an-object"
    elif case == "bare_hash":
        corrupt["hash"] = hashlib.sha256(b"lm_other").hexdigest()
    elif case == "duplicate_hash":
        corrupt["hash"] = target["hash"]
    elif case == "unknown_permission":
        corrupt["permissions"] = ["read", "unknown"]
    elif case == "duplicate_permission":
        corrupt["permissions"] = ["read", "read"]
    elif case == "invalid_space":
        corrupt["space_ids"] = ["../alpha"]
    elif case == "duplicate_space":
        corrupt["space_ids"] = ["alpha", "alpha"]
    elif case == "admin_scope":
        corrupt["permissions"] = ["admin"]
        corrupt["space_ids"] = ["alpha"]
    elif case == "revoked_not_bool":
        corrupt["revoked"] = "false"
    elif case == "expires_not_string":
        corrupt["expires_at"] = 123
    elif case == "name_not_string":
        corrupt["name"] = 123
    payload = json.dumps({"version": 2, "tokens": [target, corrupt]})
    reader = _Reader(payload)
    assert _validate(_make_validator(reader), raw) is None


# --------------------------------------------------------------------------- #
# NO-GO #8 — mono-tenant: presented credential always carries memory_ids=[]    #
# --------------------------------------------------------------------------- #

def test_returned_memory_ids_always_empty():
    # memory_ids=[] routes GM memory_create through its no-op branch, so the
    # unified credential can never trigger the token auto-add mutation.
    raw = "lm_mono"
    reader = _Reader(_store(_token_entry(raw, permissions=["admin"], space_ids=[])))
    info = _validate(_make_validator(reader), raw)
    assert info is not None
    assert list(info.memory_ids) == [], "GM credential must present empty memory_ids (mono-tenant)"


# --------------------------------------------------------------------------- #
# Fail-closed — unknown / revoked / expired → None                            #
# --------------------------------------------------------------------------- #

def test_unknown_token_rejected():
    reader = _Reader(_store(_token_entry("lm_other")))
    assert _validate(_make_validator(reader), "lm_not_in_store") is None


def test_revoked_token_rejected():
    raw = "lm_revoked"
    reader = _Reader(_store(_token_entry(raw, revoked=True)))
    assert _validate(_make_validator(reader), raw) is None


def test_expired_token_rejected():
    raw = "lm_expired"
    past = (datetime(2020, 1, 1, tzinfo=timezone.utc)).isoformat()
    reader = _Reader(_store(_token_entry(raw, expires_at=past)))
    now = lambda: datetime(2026, 6, 29, tzinfo=timezone.utc)
    assert _validate(_make_validator(reader, clock=now), raw) is None


def test_missing_tokens_store_fails_closed():
    # No tokens.json (reader returns None) → deny, never silent allow.
    reader = _Reader(None)
    assert _validate(_make_validator(reader), "lm_anything") is None


@pytest.mark.parametrize(
    "version_payload",
    [
        {},
        {"version": 1},
        {"version": 3},
        {"version": "2"},
        {"version": 2.0},
        {"version": True},
    ],
)
def test_non_current_token_store_version_fails_closed(version_payload):
    """GM never migrates/reinterprets the shared auth authority itself."""
    raw = "lm_version_guard"
    payload = {**version_payload, "tokens": [_token_entry(raw)]}
    reader = _Reader(json.dumps(payload))
    assert _validate(_make_validator(reader), raw) is None


# --------------------------------------------------------------------------- #
# NO-GO #5 — S3 signature mode mirrors Hivemind (default 'dual', not sigv4)    #
# --------------------------------------------------------------------------- #

def test_signature_mode_defaults_to_dual(monkeypatch):
    # Dell ECS Cloud Temple GETs _system/tokens.json via SigV2 (Hivemind default
    # mode 'dual'). A hardcoded sigv4 default would brick auth there.
    monkeypatch.delenv("S3_SIGNATURE_MODE", raising=False)
    reader = _Reader(_store())
    v = _make_validator(reader)  # no explicit mode, no env → must default to 'dual'
    assert getattr(v, "signature_mode", None) == "dual"


def test_signature_mode_sigv4_opt_in_respected():
    reader = _Reader(_store())
    v = _make_validator(reader, signature_mode="sigv4")
    assert v.signature_mode == "sigv4"


def test_signature_mode_mirrors_hivemind_env(monkeypatch):
    # Codex finding #3: the validator must MIRROR Hivemind's S3_SIGNATURE_MODE
    # env (single source of truth), NOT a separate GM-only knob — else an
    # operator on MinIO/AWS (sigv4) gets a SigV2 GET and auth bricks.
    monkeypatch.setenv("S3_SIGNATURE_MODE", "sigv4")
    reader = _Reader(_store())
    v = _make_validator(reader)  # no explicit mode → reads the env
    assert v.signature_mode == "sigv4"


# --------------------------------------------------------------------------- #
# NO-GO #9 — cache is fail-closed: positive-only, re-checks expiry on hit      #
# --------------------------------------------------------------------------- #

def test_negative_lookups_are_not_cached():
    # An unknown token must not be cached as a negative: after it is added to the
    # store, the very next call must succeed (no poisoned negative cache).
    raw = "lm_added_later"
    reader = _Reader(_store(_token_entry("lm_someone_else")))
    v = _make_validator(reader)
    assert _validate(v, raw) is None
    reader.payload = _store(_token_entry("lm_someone_else"), _token_entry(raw))
    assert _validate(v, raw) is not None, "negative results must not be cached"


def test_expiry_rechecked_every_call():
    # Codex finding #1: the validator re-reads + re-validates the store on every
    # call (no positive cache that could grant on stale data). A token that
    # expires between calls is rejected on the next call.
    raw = "lm_soon_expiring"
    expires = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
    reader = _Reader(_store(_token_entry(raw, expires_at=expires.isoformat())))
    t = {"now": datetime(2026, 6, 29, 11, 0, 0, tzinfo=timezone.utc)}
    v = _make_validator(reader, clock=lambda: t["now"], cache_ttl_seconds=86400)
    assert _validate(v, raw) is not None, "valid before expiry"
    t["now"] = datetime(2026, 6, 29, 12, 0, 1, tzinfo=timezone.utc)
    assert _validate(v, raw) is None, "expiry must be re-checked on the next call"


def test_no_grant_after_store_becomes_unreadable():
    # Codex finding #1: a previously-valid token must be DENIED once the store is
    # unreadable/empty — never served from a cache.
    raw = "lm_live_then_gone"
    reader = _Reader(_store(_token_entry(raw)))
    v = _make_validator(reader, cache_ttl_seconds=86400)
    assert _validate(v, raw) is not None
    reader.payload = None  # store now unreadable/empty
    assert _validate(v, raw) is None, "must fail closed when the store is unreadable"


def test_revocation_takes_effect_immediately():
    # Codex finding #1: revoking in the store denies on the very next call.
    raw = "lm_revoke_live"
    reader = _Reader(_store(_token_entry(raw)))
    v = _make_validator(reader, cache_ttl_seconds=86400)
    assert _validate(v, raw) is not None
    reader.payload = _store(_token_entry(raw, revoked=True))
    assert _validate(v, raw) is None, "revocation must not be masked by a cache"


def test_deleted_token_denied_immediately():
    # Codex finding #1: a token removed from the store stops working at once.
    raw = "lm_removed_live"
    reader = _Reader(_store(_token_entry(raw)))
    v = _make_validator(reader, cache_ttl_seconds=86400)
    assert _validate(v, raw) is not None
    reader.payload = _store(_token_entry("lm_someone_else"))  # raw removed
    assert _validate(v, raw) is None, "a deleted token must not keep working"


def test_malformed_expires_at_fails_closed():
    # Codex finding #4: a present-but-unparseable expires_at must DENY, not be
    # treated as "no expiry".
    raw = "lm_bad_exp"
    reader = _Reader(_store(_token_entry(raw, expires_at="not-a-date")))
    assert _validate(_make_validator(reader), raw) is None, "corrupt expires_at must fail closed"


def test_nonboolean_revoked_fails_closed():
    # Codex finding #4: a non-boolean (corrupt) revoked value must DENY.
    raw = "lm_bad_revoked"
    entry = _token_entry(raw)
    entry["revoked"] = "yes"  # non-boolean truthy
    reader = _Reader(_store(entry))
    assert _validate(_make_validator(reader), raw) is None, "non-boolean revoked must fail closed"


# --------------------------------------------------------------------------- #
# NO-GO #4a — context fails CLOSED: auth is None must DENY (was allow)         #
# --------------------------------------------------------------------------- #

def test_context_checks_deny_when_auth_is_none():
    from mcp_memory.auth import context

    tok = context.current_auth.set(None)
    try:
        assert context.check_admin_permission() is not None, "admin check must deny on auth=None"
        assert context.check_write_permission() is not None, "write check must deny on auth=None"
        assert context.check_memory_access("some-mem") is not None, "memory access must deny on auth=None"
    finally:
        context.current_auth.reset(tok)


def test_get_allowed_memory_ids_denies_on_no_auth():
    # Codex finding #2: the list helper must NOT conflate no-auth with admin —
    # memory_list / backup_list deny instead of listing everything.
    from mcp_memory.auth import context

    tok = context.current_auth.set(None)
    try:
        assert context.get_allowed_memory_ids() is context.DENY_ALL, (
            "no auth context => DENY_ALL (list helpers must show nothing)"
        )
    finally:
        context.current_auth.reset(tok)
    tok = context.current_auth.set(
        {"client_name": "a", "permissions": ["admin"], "memory_ids": []}
    )
    try:
        assert context.get_allowed_memory_ids() is None, "admin stays unrestricted (None)"
    finally:
        context.current_auth.reset(tok)


# --------------------------------------------------------------------------- #
# NO-GO #4b/#4c, #7, #9 — middleware patches (source assertions; neo4j-heavy)  #
# --------------------------------------------------------------------------- #

def _middleware_source() -> str:
    return _MIDDLEWARE_SRC.read_text(encoding="utf-8")


def test_localhost_bypass_defaults_to_false_in_code():
    src = _middleware_source()
    assert 'os.getenv("LOCALHOST_AUTH_BYPASS", "true")' not in src, (
        "LOCALHOST_AUTH_BYPASS must NOT default to 'true' (NO-GO #4b)"
    )
    assert 'os.getenv("LOCALHOST_AUTH_BYPASS", "false")' in src, (
        "LOCALHOST_AUTH_BYPASS must default to 'false' (fail-closed)"
    )


def test_no_wildcard_cors_header():
    src = _middleware_source()
    assert '(b"access-control-allow-origin", b"*")' not in src, (
        "wildcard CORS 'access-control-allow-origin: *' must be removed (NO-GO #4c)"
    )


def test_live_auth_path_uses_s3_validator_not_neo4j_token_manager():
    src = _middleware_source()
    assert "s3_token_validator" in src or "S3TokenValidator" in src, (
        "middleware must call the S3 token validator (Model B / NO-GO #7)"
    )
    assert "self.token_manager.validate_token" not in src, (
        "Neo4j token_manager.validate_token must be REMOVED from the live auth path (NO-GO #7)"
    )


def test_contextvar_is_reset_after_request():
    src = _middleware_source()
    assert "current_auth.reset" in src, (
        "current_auth must be reset() after the request to avoid cross-session bleed (NO-GO #9)"
    )
