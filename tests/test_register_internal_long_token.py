# -*- coding: utf-8 -*-
"""
P7-3 — TokenService.register_internal_long_token (register-by-hash + rotation).

- enregistre le hash d'un plaintext DÉJÀ résolu (least-privilege read+write) ;
- idempotent (même plaintext → no-op, aucune écriture supplémentaire) ;
- rotation : garantit UN SEUL token actif au nom réservé (révoque les autres) ;
- JAMAIS un token opérateur (autre nom) ;
- respecte un revoke opérateur explicite (pas de ré-activation).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import patch

from live_mem.core.tokens import TokenService, TOKENS_KEY
from live_mem.core.models import INTERNAL_LONG_TOKEN_NAME


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, str] = {}
        self.put_json_calls = 0

    async def get_json(self, key: str):
        raw = self.objects.get(key)
        return None if raw is None else json.loads(raw)

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        self.put_json_calls += 1
        self.objects[key] = json.dumps(data)

    def tokens(self) -> list[dict]:
        return json.loads(self.objects[TOKENS_KEY])["tokens"]


def _hash(raw: str) -> str:
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def _patch(storage):
    return patch("live_mem.core.tokens.get_storage", return_value=storage)


async def test_registers_scoped_readwrite_not_admin() -> None:
    storage = FakeStorage()
    with _patch(storage):
        res = await TokenService().register_internal_long_token("tok-A")
    assert res["status"] == "ok" and res["registered"] is True
    assert res["current_active"] is True
    toks = storage.tokens()
    assert len(toks) == 1
    e = toks[0]
    assert e["hash"] == _hash("tok-A")
    assert e["name"] == INTERNAL_LONG_TOKEN_NAME
    assert set(e["permissions"]) == {"read", "write"}
    assert "admin" not in e["permissions"]
    assert e["space_ids"] == []
    assert e["revoked"] is False


async def test_existing_current_token_scope_is_normalized_to_empty() -> None:
    """The reserved internal credential never retains a space allowlist."""
    storage = FakeStorage()
    service = TokenService()
    current = service._store_from_data(
        {
            "version": 2,
            "tokens": [
                {
                    "hash": _hash("tok-A"),
                    "name": INTERNAL_LONG_TOKEN_NAME,
                    "permissions": ["read", "write"],
                    "space_ids": ["legacy-space"],
                    "revoked": False,
                }
            ],
        }
    )

    async def _load_current():
        return current

    async def _save_current(store):
        storage.objects[TOKENS_KEY] = json.dumps(store.model_dump())
        storage.put_json_calls += 1

    with patch.object(service, "_load_store", _load_current), patch.object(
        service, "_save_store", _save_current
    ):
        res = await service.register_internal_long_token("tok-A")

    assert res["status"] == "ok"
    assert res["registered"] is False
    assert res["scopes_normalized"] == 1
    assert storage.tokens()[0]["space_ids"] == []


async def test_idempotent_same_token_no_extra_write() -> None:
    storage = FakeStorage()
    with _patch(storage):
        svc = TokenService()
        await svc.register_internal_long_token("tok-A")
        writes_after_first = storage.put_json_calls
        res2 = await svc.register_internal_long_token("tok-A")
    assert res2["registered"] is False
    assert res2["current_active"] is True
    assert res2["rotated_out"] == 0
    assert len(storage.tokens()) == 1
    # Aucune écriture supplémentaire (no-op strict).
    assert storage.put_json_calls == writes_after_first


async def test_rotation_revokes_stale_keeps_one_active() -> None:
    storage = FakeStorage()
    with _patch(storage):
        svc = TokenService()
        await svc.register_internal_long_token("tok-A")
        res = await svc.register_internal_long_token("tok-B")  # rotation
    assert res["rotated_out"] == 1
    toks = storage.tokens()
    active = [t for t in toks if t["name"] == INTERNAL_LONG_TOKEN_NAME and not t["revoked"]]
    assert len(active) == 1
    assert active[0]["hash"] == _hash("tok-B")
    # L'ancien est révoqué (pas orphelin actif).
    stale = [t for t in toks if t["hash"] == _hash("tok-A")]
    assert stale and stale[0]["revoked"] is True


async def test_never_touches_operator_token() -> None:
    storage = FakeStorage()
    # Pré-remplir un token opérateur (autre nom) actif.
    storage.objects[TOKENS_KEY] = json.dumps(
        {
            "version": 2,
            "tokens": [
                {
                    "hash": _hash("operator-secret"),
                    "name": "agent-cline",
                    "permissions": ["read", "write", "admin"],
                    # Token-store v2 invariant: admin access is global and
                    # dormant allowlists are forbidden.
                    "space_ids": [],
                    "revoked": False,
                }
            ],
        }
    )
    with _patch(storage):
        await TokenService().register_internal_long_token("tok-A")
    op = [t for t in storage.tokens() if t["name"] == "agent-cline"]
    assert len(op) == 1 and op[0]["revoked"] is False  # intact
    assert op[0]["permissions"] == ["read", "write", "admin"]
    assert op[0]["space_ids"] == []


async def test_respects_operator_revoke_no_reactivation() -> None:
    storage = FakeStorage()
    # Le token interne courant a été révoqué manuellement par l'opérateur.
    storage.objects[TOKENS_KEY] = json.dumps(
        {
            "version": 2,
            "tokens": [
                {
                    "hash": _hash("tok-A"),
                    "name": INTERNAL_LONG_TOKEN_NAME,
                    "permissions": ["read", "write"],
                    "space_ids": [],
                    "revoked": True,
                }
            ],
        }
    )
    with _patch(storage):
        res = await TokenService().register_internal_long_token("tok-A")
    # Pas de ré-activation (respecte l'intention opérateur) ; pas de doublon.
    entry = [t for t in storage.tokens() if t["hash"] == _hash("tok-A")]
    assert len(entry) == 1 and entry[0]["revoked"] is True
    assert res["registered"] is False
    assert res["current_active"] is False
    assert res["rotated_out"] == 0
    assert storage.put_json_calls == 0


async def test_revoked_exact_hash_does_not_revoke_active_replacement() -> None:
    storage = FakeStorage()
    storage.objects[TOKENS_KEY] = json.dumps(
        {
            "version": 2,
            "tokens": [
                {
                    "hash": _hash("tok-B"),
                    "name": INTERNAL_LONG_TOKEN_NAME,
                    "permissions": ["read", "write"],
                    "space_ids": [],
                    "revoked": True,
                },
                {
                    "hash": _hash("tok-C"),
                    "name": INTERNAL_LONG_TOKEN_NAME,
                    "permissions": ["read", "write"],
                    "space_ids": [],
                    "revoked": False,
                },
            ],
        },
        sort_keys=True,
    )
    before = storage.objects[TOKENS_KEY]
    with _patch(storage):
        result = await TokenService().register_internal_long_token("tok-B")
    assert result["current_active"] is False
    assert result["rotated_out"] == 0
    assert storage.put_json_calls == 0
    assert storage.objects[TOKENS_KEY] == before
    active = [token for token in storage.tokens() if not token["revoked"]]
    assert [token["hash"] for token in active] == [_hash("tok-C")]


async def test_expired_exact_hash_is_zero_mutation() -> None:
    storage = FakeStorage()
    storage.objects[TOKENS_KEY] = json.dumps(
        {
            "version": 2,
            "tokens": [
                {
                    "hash": _hash("tok-old"),
                    "name": INTERNAL_LONG_TOKEN_NAME,
                    "permissions": ["read", "write", "admin"],
                    "space_ids": [],
                    "expires_at": "2000-01-01T00:00:00+00:00",
                    "revoked": False,
                },
                {
                    "hash": _hash("tok-current"),
                    "name": INTERNAL_LONG_TOKEN_NAME,
                    "permissions": ["read", "write"],
                    "space_ids": [],
                    "revoked": False,
                },
            ],
        },
        sort_keys=True,
    )
    before = storage.objects[TOKENS_KEY]
    with _patch(storage):
        result = await TokenService().register_internal_long_token("tok-old")
    assert result == {
        "status": "ok",
        "name": INTERNAL_LONG_TOKEN_NAME,
        "registered": False,
        "current_active": False,
        "rotated_out": 0,
        "permissions_normalized": 0,
        "scopes_normalized": 0,
    }
    assert storage.put_json_calls == 0
    assert storage.objects[TOKENS_KEY] == before
