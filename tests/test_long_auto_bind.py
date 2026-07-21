# -*- coding: utf-8 -*-
"""
P7-3 — auto-bind interne du runtime long embarqué (ADR-0019).

Drive le VRAI GraphBridgeService via le seam client (FakeGraphTransport) sur un
FakeStorage in-memory. Aucun réseau / S3 / Neo4j / Qdrant / LLM.

Couvre les résolutions Codex (3 rounds) :
- provision UNIQUEMENT au 1er long_push (status reste read-only, ne mute rien) ;
- token embarqué VIVANT jamais persisté (sentinel at-rest → backups bruts sûrs) ;
- binding EXPLICITE (embedded|explicit), jamais inféré depuis url/token ;
- garde SSRF sur CHAQUE URL résolue (embedded + explicite persisté) ;
- race memory_create "existe déjà" (dict d'erreur MCP) → succès ;
- fail-closed : health malformé / secret absent / URL embedded incohérente ;
- token interne enregistré read+write (jamais admin) via le store S3 (Model B).
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest

from live_mem.config import Settings
from live_mem.core.graph_bridge import (
    GraphBridgeService,
    _BINDING_EMBEDDED,
    _BINDING_EXPLICIT,
)
from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL, INTERNAL_LONG_TOKEN_NAME
from live_mem.core.memory_id import derive_memory_id
from live_mem.core.tokens import TOKENS_KEY
from tests.fakes import FakeGraphTransport

_SPACE = "space-a"
_META = f"{_SPACE}/_meta.json"
_EMBEDDED_URL = "http://graph-memory:8002"
_EMBEDDED_TOKEN = "tok-embedded-xyz"


class FakeStorage:
    """In-memory storage : get/put(_json) + list_and_get (bank) + list/copy (backup)."""

    def __init__(self, bank: dict[str, str] | None = None) -> None:
        self.objects: dict[str, str] = {}
        # bank : relpath -> content, exposés sous {space}/bank/<relpath>
        self._bank = bank or {}

    async def put(self, key: str, content: str, content_type: str = "text/plain") -> None:
        self.objects[key] = content

    async def put_json(self, key: str, data: dict[str, Any]) -> None:
        await self.put(key, json.dumps(data, indent=2, ensure_ascii=False))

    async def get(self, key: str):
        return self.objects.get(key)

    async def get_json(self, key: str):
        raw = await self.get(key)
        return None if raw is None else json.loads(raw)

    async def list_and_get(self, prefix: str, exclude_keep: bool = True) -> list[dict]:
        out = []
        for rel, content in self._bank.items():
            out.append({"key": f"{_SPACE}/bank/{rel}", "content": content})
        return out

    def snapshot(self) -> dict[str, str]:
        return deepcopy(self.objects)

    def raw_meta(self) -> dict:
        return json.loads(self.objects[_META])


def _settings(**kw) -> Settings:
    base = dict(
        long_embedded_url=_EMBEDDED_URL,
        long_embedded_token=_EMBEDDED_TOKEN,
        long_embedded_token_file="/nonexistent/never-written",
    )
    base.update(kw)
    return Settings(**base)


def _bridge(**factory_kwargs):
    factory = FakeGraphTransport.factory(**factory_kwargs)
    return GraphBridgeService(client_factory=factory), factory


def _patches(storage: FakeStorage, settings: Settings):
    """Patch storage (bridge+tokens) + settings (bridge)."""
    return (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.tokens.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=settings),
    )


def _space_meta() -> dict:
    return {"space_id": _SPACE, "version": 1}


async def _run(coro, storage, settings):
    p1, p2, p3 = _patches(storage, settings)
    with p1, p2, p3:
        return await coro()


# ─────────────────────────────────────────────────────────────
# status : READ-ONLY — ne provisionne jamais, ne mute rien
# ─────────────────────────────────────────────────────────────


async def test_status_unbound_reports_embedded_without_write_or_network() -> None:
    storage = FakeStorage()
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, factory = _bridge()

    res = await _run(lambda: bridge.status(_SPACE), storage, settings)

    assert res["connected"] is False
    assert res.get("bound") is False
    assert res.get("embedded") is True
    # AUCUN client construit (aucun réseau), AUCUNE écriture (pas de bloc, pas de token).
    assert factory.instances == []
    assert "graph_memory" not in storage.raw_meta()
    assert TOKENS_KEY not in storage.objects


# ─────────────────────────────────────────────────────────────
# push : auto-bind au 1er write
# ─────────────────────────────────────────────────────────────


async def test_first_push_provisions_embedded_bind() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, factory = _bridge()

    res = await _run(lambda: bridge.push(_SPACE), storage, settings)
    assert res["status"] == "ok"

    block = storage.raw_meta()["graph_memory"]
    assert block["binding"] == _BINDING_EMBEDDED
    assert block["url"] == _EMBEDDED_URL
    assert block["memory_id"] == derive_memory_id(_SPACE)
    # memory_create appelé avec le memory_id dérivé.
    created = [c for inst in factory.instances for c in inst.args_for("memory_create")]
    assert any(a.get("memory_id") == derive_memory_id(_SPACE) for a in created)


async def test_autobind_persists_sentinel_not_literal_token() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, _ = _bridge()

    await _run(lambda: bridge.push(_SPACE), storage, settings)

    # Le token VIVANT n'apparaît NULLE PART dans le _meta.json au repos
    # (donc jamais copié tel quel dans un backup brut de _meta.json).
    raw = storage.objects[_META]
    assert _EMBEDDED_TOKEN not in raw
    assert storage.raw_meta()["graph_memory"]["token"] == EMBEDDED_TOKEN_SENTINEL


async def test_second_push_keeps_sentinel_and_no_recreate() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, factory = _bridge()

    await _run(lambda: bridge.push(_SPACE), storage, settings)
    await _run(lambda: bridge.push(_SPACE), storage, settings)

    assert _EMBEDDED_TOKEN not in storage.objects[_META]
    assert storage.raw_meta()["graph_memory"]["token"] == EMBEDDED_TOKEN_SENTINEL
    # memory_create appelé UNE seule fois au total (idempotent : le 2e push voit
    # le binding persisté et ne re-crée pas).
    total_creates = sum(len(inst.args_for("memory_create")) for inst in factory.instances)
    assert total_creates == 1


async def test_push_then_status_reports_connected() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, _ = _bridge()

    await _run(lambda: bridge.push(_SPACE), storage, settings)
    res = await _run(lambda: bridge.status(_SPACE), storage, settings)
    assert res["connected"] is True
    assert res["config"]["memory_id"] == derive_memory_id(_SPACE)
    # status ne fuit jamais le token.
    assert EMBEDDED_TOKEN_SENTINEL not in json.dumps(res)
    assert _EMBEDDED_TOKEN not in json.dumps(res)


# ─────────────────────────────────────────────────────────────
# maintenance : override legacy -> runtime embedded, sans push
# ─────────────────────────────────────────────────────────────


async def test_use_embedded_replaces_legacy_override_without_ingestion_or_delete() -> None:
    storage = FakeStorage()
    meta = _space_meta()
    meta["description"] = "preserve me"
    meta["graph_memory"] = {
        "binding": _BINDING_EXPLICIT,
        "url": "https://legacy-gm.example.com",
        "token": "legacy-secret-must-not-leak",
        "memory_id": "legacy-memory",
        "ontology": "general",
        "push_count": 3,
    }
    storage.objects[_META] = json.dumps(meta)
    settings = _settings()
    bridge, factory = _bridge()

    res = await _run(
        lambda: bridge.disconnect(_SPACE, use_embedded=True), storage, settings
    )

    assert res["status"] == "connected"
    assert res["binding"] == _BINDING_EMBEDDED
    assert res["previous_graph_memory"] == {
        "binding": _BINDING_EXPLICIT,
        "url": "https://legacy-gm.example.com",
        "memory_id": "legacy-memory",
        "push_count": 3,
    }
    assert "legacy-secret-must-not-leak" not in json.dumps(res)

    persisted = storage.raw_meta()
    assert persisted["description"] == "preserve me"
    assert persisted["graph_memory"] == {
        "binding": _BINDING_EMBEDDED,
        "url": _EMBEDDED_URL,
        "token": EMBEDDED_TOKEN_SENTINEL,
        "memory_id": derive_memory_id(_SPACE),
        "ontology": "general",
    }
    assert "legacy-secret-must-not-leak" not in storage.objects[_META]

    names = [name for inst in factory.instances for name in inst.tool_names()]
    assert names == ["system_health", "memory_list", "memory_create"]
    assert "memory_ingest" not in names
    assert "document_delete" not in names
    assert "memory_delete" not in names


async def test_use_embedded_failure_preserves_legacy_binding_byte_for_byte() -> None:
    storage = FakeStorage()
    meta = _space_meta()
    meta["graph_memory"] = {
        "binding": _BINDING_EXPLICIT,
        "url": "https://legacy-gm.example.com",
        "token": "legacy-secret",
        "memory_id": "legacy-memory",
        "ontology": "general",
        "push_count": 7,
    }
    storage.objects[_META] = json.dumps(meta, sort_keys=True)
    before = storage.objects[_META]
    settings = _settings()
    bridge, factory = _bridge(
        responses={"system_health": {"status": "error", "message": "down"}}
    )

    res = await _run(
        lambda: bridge.disconnect(_SPACE, use_embedded=True), storage, settings
    )

    assert res["status"] == "error"
    assert res["previous_binding_preserved"] is True
    assert storage.objects[_META] == before
    assert "legacy-secret" not in json.dumps(res)
    names = [name for inst in factory.instances for name in inst.tool_names()]
    assert names == ["system_health"]


async def test_use_embedded_refuses_to_overwrite_concurrent_binding_change() -> None:
    class DriftingStorage(FakeStorage):
        def __init__(self) -> None:
            super().__init__()
            self.meta_reads = 0

        async def get_json(self, key: str):
            data = await super().get_json(key)
            if key == _META:
                self.meta_reads += 1
                if self.meta_reads == 2 and isinstance(data, dict):
                    data["graph_memory"] = {
                        "binding": _BINDING_EXPLICIT,
                        "url": "https://concurrent.example.com",
                        "token": "concurrent-secret",
                        "memory_id": "concurrent-memory",
                    }
                    self.objects[_META] = json.dumps(data)
            return data

    storage = DriftingStorage()
    meta = _space_meta()
    meta["graph_memory"] = {
        "binding": _BINDING_EXPLICIT,
        "url": "https://legacy-gm.example.com",
        "token": "legacy-secret",
        "memory_id": "legacy-memory",
    }
    storage.objects[_META] = json.dumps(meta)
    settings = _settings()
    bridge, _ = _bridge()

    res = await _run(
        lambda: bridge.disconnect(_SPACE, use_embedded=True), storage, settings
    )

    assert res["status"] == "error"
    assert "a changé" in res["message"]
    assert storage.raw_meta()["graph_memory"]["memory_id"] == "concurrent-memory"


# ─────────────────────────────────────────────────────────────
# fail-closed
# ─────────────────────────────────────────────────────────────


async def test_missing_embedded_service_fail_closed_no_write() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, factory = _bridge(responses={"system_health": {"status": "error"}})

    res = await _run(lambda: bridge.push(_SPACE), storage, settings)
    assert res["connected"] is False
    # Aucun bloc "bound" persisté.
    assert "graph_memory" not in storage.raw_meta()


async def test_malformed_health_fail_closed() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    # Réponse sans clé "status" → NE DOIT PAS être traitée comme healthy.
    bridge, _ = _bridge(responses={"system_health": {"weird": "shape"}})

    res = await _run(lambda: bridge.push(_SPACE), storage, settings)
    assert res.get("connected") is False
    assert "graph_memory" not in storage.raw_meta()


async def test_empty_env_token_read_path_fail_closed() -> None:
    # Bloc embedded restauré, mais aucun secret résolvable (env vide + fichier
    # inexistant) sur le chemin LECTURE (status) → fail-closed, aucun client.
    storage = FakeStorage()
    meta = _space_meta()
    meta["graph_memory"] = {
        "binding": _BINDING_EMBEDDED,
        "url": _EMBEDDED_URL,
        "token": EMBEDDED_TOKEN_SENTINEL,
        "memory_id": derive_memory_id(_SPACE),
        "ontology": "general",
    }
    storage.objects[_META] = json.dumps(meta)
    settings = _settings(long_embedded_token="")  # pas de secret résolvable
    bridge, factory = _bridge()

    res = await _run(lambda: bridge.status(_SPACE), storage, settings)
    assert res.get("connected") is False
    assert factory.instances == []  # jamais de client avec token=""


async def test_embedded_marker_url_mismatch_fail_closed() -> None:
    # binding=embedded mais URL persistée != URL embarquée → ne JAMAIS injecter
    # le token embarqué vers une URL non-embarquée (anti-leak).
    storage = FakeStorage()
    meta = _space_meta()
    meta["graph_memory"] = {
        "binding": _BINDING_EMBEDDED,
        "url": "http://evil.example.com:9999",
        "token": EMBEDDED_TOKEN_SENTINEL,
        "memory_id": derive_memory_id(_SPACE),
        "ontology": "general",
    }
    storage.objects[_META] = json.dumps(meta)
    settings = _settings()
    bridge, factory = _bridge()

    res = await _run(lambda: bridge.status(_SPACE), storage, settings)
    assert res["status"] == "error"
    assert factory.instances == []


# ─────────────────────────────────────────────────────────────
# race : memory_create renvoie "existe déjà" en DICT d'erreur MCP
# ─────────────────────────────────────────────────────────────


async def test_memory_create_exists_errordict_converges() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    mid = derive_memory_id(_SPACE)
    # 1er memory_list : vide → tente create ; create → erreur "existe déjà" ;
    # re-check memory_list : présent → succès.
    responses = {
        "memory_list": [
            {"status": "ok", "memories": []},
            {"status": "ok", "memories": [{"memory_id": mid}]},
        ],
        "memory_create": {"status": "error", "message": f"La mémoire '{mid}' existe déjà"},
    }
    bridge, _ = _bridge(responses=responses)

    res = await _run(lambda: bridge.push(_SPACE), storage, settings)
    assert res["status"] == "ok"
    assert storage.raw_meta()["graph_memory"]["memory_id"] == mid


# ─────────────────────────────────────────────────────────────
# token interne : enregistré scopé read+write (Model B), jamais admin
# ─────────────────────────────────────────────────────────────


async def test_internal_token_registered_scoped_readwrite() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, _ = _bridge()

    await _run(lambda: bridge.push(_SPACE), storage, settings)

    store = json.loads(storage.objects[TOKENS_KEY])
    internal = [t for t in store["tokens"] if t["name"] == INTERNAL_LONG_TOKEN_NAME]
    assert len(internal) == 1
    entry = internal[0]
    assert entry["hash"] == "sha256:" + hashlib.sha256(_EMBEDDED_TOKEN.encode()).hexdigest()
    assert set(entry["permissions"]) == {"read", "write"}
    assert "admin" not in entry["permissions"]
    assert entry["revoked"] is False


async def test_provision_registers_token_before_any_gm_call() -> None:
    # Ordering (Codex R1) : l'enregistrement du token PRÉCÈDE tout appel GM
    # authentifié. Preuve : si register échoue, AUCUN client GM n'est construit
    # et AUCUN bloc "bound" n'est persisté (fail-closed avant contact GM).
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, factory = _bridge()

    class _FailingTokens:
        async def register_internal_long_token(self, *a, **k):
            return {"status": "error", "message": "boom"}

    p1, p2, p3 = _patches(storage, settings)
    with p1, p2, p3, patch(
        "live_mem.core.tokens.get_token_service", return_value=_FailingTokens()
    ):
        res = await bridge.push(_SPACE)

    assert res.get("connected") is False
    assert factory.instances == []  # aucun client GM construit → register d'abord
    assert "graph_memory" not in storage.raw_meta()  # aucun bloc bound persisté


async def test_provision_rejects_inactive_exact_internal_token() -> None:
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, factory = _bridge()

    class _InactiveTokens:
        async def register_internal_long_token(self, *a, **k):
            return {
                "status": "ok",
                "registered": False,
                "current_active": False,
                "rotated_out": 0,
            }

    p1, p2, p3 = _patches(storage, settings)
    with p1, p2, p3, patch(
        "live_mem.core.tokens.get_token_service", return_value=_InactiveTokens()
    ):
        res = await bridge.push(_SPACE)

    assert res.get("connected") is False
    assert "inactif" in res.get("message", "")
    assert factory.instances == []
    assert "graph_memory" not in storage.raw_meta()


async def test_status_does_not_register_token() -> None:
    # status (read) ne provisionne pas → aucun token interne enregistré.
    storage = FakeStorage()
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, _ = _bridge()

    await _run(lambda: bridge.status(_SPACE), storage, settings)
    assert TOKENS_KEY not in storage.objects


# ─────────────────────────────────────────────────────────────
# override explicite + SSRF
# ─────────────────────────────────────────────────────────────


async def test_explicit_connect_marks_explicit_and_overrides_embedded() -> None:
    storage = FakeStorage()
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, _ = _bridge()

    await _run(
        lambda: bridge.connect(
            _SPACE, url="https://ops.example.com", token="op-token", memory_id="op-mem"
        ),
        storage,
        settings,
    )
    block = storage.raw_meta()["graph_memory"]
    assert block["binding"] == _BINDING_EXPLICIT
    assert block["token"] == "op-token"  # token opérateur conservé (masqué à l'egress)
    assert block["url"] == "https://ops.example.com"

    res = await _run(lambda: bridge.status(_SPACE), storage, settings)
    assert res["config"]["url"] == "https://ops.example.com"
    assert res["config"]["memory_id"] == "op-mem"


async def test_connect_rejects_token_equal_to_sentinel() -> None:
    storage = FakeStorage()
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, factory = _bridge()

    res = await _run(
        lambda: bridge.connect(
            _SPACE, url="https://ops.example.com", token=EMBEDDED_TOKEN_SENTINEL, memory_id="m"
        ),
        storage,
        settings,
    )
    assert res["status"] == "error"
    assert factory.instances == []
    assert "graph_memory" not in storage.raw_meta()


async def test_resolver_guards_persisted_explicit_ssrf_url_on_status() -> None:
    # Un bloc explicite persisté avec une URL RFC-1918 doit être refusé AVANT
    # toute construction de client (push ET status), pas seulement à connect.
    storage = FakeStorage(bank={"projectbrief.md": "hello"})
    meta = _space_meta()
    meta["graph_memory"] = {
        "binding": _BINDING_EXPLICIT,
        "url": "http://169.254.169.254",
        "token": "op-token",
        "memory_id": "op-mem",
        "ontology": "general",
    }
    storage.objects[_META] = json.dumps(meta)
    settings = _settings()

    bridge, factory = _bridge()
    res = await _run(lambda: bridge.status(_SPACE), storage, settings)
    assert res["status"] == "error"
    assert factory.instances == []

    bridge2, factory2 = _bridge()
    res2 = await _run(lambda: bridge2.push(_SPACE), storage, settings)
    assert res2["status"] == "error"
    assert factory2.instances == []


async def test_connect_explicit_loopback_ssrf_refused() -> None:
    storage = FakeStorage()
    storage.objects[_META] = json.dumps(_space_meta())
    settings = _settings()
    bridge, factory = _bridge()

    res = await _run(
        lambda: bridge.connect(
            _SPACE, url="http://127.0.0.1:8002", token="op-token", memory_id="m"
        ),
        storage,
        settings,
    )
    assert res["status"] == "error"
    assert factory.instances == []
