# -*- coding: utf-8 -*-
"""
P7-8 (#124) — security & backup review of the embedded Graph Memory state.

Four risk areas, each locked by a mutation-proven guard:

1. **document_delete is document_id-keyed (latent data-path bug).** The real
   GM tool (`services/graph-memory/src/mcp_memory/server.py`) requires a
   ``document_id`` (UUID); the pre-P7-8 ``push()`` passed ``filename`` — every
   delete silently failed against a real GM (masked by the then-permissive
   ``FakeGraphTransport``). The bridge now resolves ids from ``document_list``
   and NEVER deletes without positive id evidence (fail-closed skip).
   The fake now enforces the real contract (strict ``document_delete``), so
   the masking can never return.

2. **GM document_delete write-gate.** Deletion is a destructive mutation:
   the GM tool must gate ``check_write_permission`` after
   ``check_memory_access`` and before any deletion (AST assertion — the GM
   server module is not importable in the Hivemind venv, same technique as
   the P7-4 middleware guards).

3. **Internal token = EXACTLY {read, write}.** ``register_internal_long_token``
   rejects manage/admin (and any non-exact set) fail-closed — the internal
   credential can never reach GM's manage/admin surfaces.

4. **Backups.** A raw ``BackupService.create()`` snapshot of an
   embedded-bound space carries the ``__embedded__`` sentinel, never the live
   internal token (sentinel-at-rest, P7-3 §4.2 of docs/SECURITY.md); the
   Hivemind backup/restore module has NO Graph Memory client edge
   (structural: restoring Hivemind protocol state can never consume long
   graph state); and docs/SECURITY.md documents the embedded trust boundary,
   the internal token handling, and the GM-native backup surface as
   long-runtime-only (never Hivemind recovery truth).

Offline, deterministic, fake-backed — no network, no S3, no Neo4j/Qdrant.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from live_mem.config import Settings
from live_mem.core.backup import BackupService
from live_mem.core.graph_bridge import GraphBridgeService
from live_mem.core.models import EMBEDDED_TOKEN_SENTINEL
from live_mem.core.tokens import TokenService
from tests.fakes import FakeGraphTransport

REPO_ROOT = Path(__file__).resolve().parents[1]
GM_SERVER = (
    REPO_ROOT / "services" / "graph-memory" / "src" / "mcp_memory" / "server.py"
)

_SPACE = "space-a"
_META = f"{_SPACE}/_meta.json"
_EMBEDDED_URL = "http://graph-memory:8002"
_EMBEDDED_TOKEN = "tok-embedded-p78-secret"


# ─────────────────────────────────────────────────────────────
# Harness (same seams as tests/test_long_auto_bind.py, + backup surface)
# ─────────────────────────────────────────────────────────────


class FakeStorage:
    """In-memory storage: bridge reads/writes + BackupService list/copy."""

    def __init__(self, bank: dict[str, str] | None = None) -> None:
        self.objects: dict[str, str] = {}
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
        return [
            {"key": f"{_SPACE}/bank/{rel}", "content": content}
            for rel, content in self._bank.items()
        ]

    # BackupService surface
    async def exists(self, key: str) -> bool:
        return key in self.objects

    async def list_objects(self, prefix: str) -> list[dict]:
        return [
            {"Key": key, "Size": len(content)}
            for key, content in self.objects.items()
            if key.startswith(prefix)
        ]

    async def copy_object(self, source_key: str, dest_key: str) -> None:
        self.objects[dest_key] = self.objects[source_key]


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
    return (
        patch("live_mem.core.graph_bridge.get_storage", return_value=storage),
        patch("live_mem.core.tokens.get_storage", return_value=storage),
        patch("live_mem.core.graph_bridge.get_settings", return_value=settings),
    )


async def _run(coro, storage: FakeStorage, settings: Settings):
    p1, p2, p3 = _patches(storage, settings)
    with p1, p2, p3:
        return await coro()


def _explicit_meta(bank_mirror: list[str] | None = None) -> dict:
    """A space bound EXPLICITLY (operator graph_connect) — real token at rest."""
    gm = {
        "url": "https://gm.example.com",
        "token": "operator-token",
        "memory_id": "mem-1",
        "ontology": "general",
        "binding": "explicit",
    }
    if bank_mirror is not None:
        gm["bank_mirror"] = bank_mirror
    return {"space_id": _SPACE, "version": 1, "graph_memory": gm}


def _doc(filename: str, doc_id: str | None, source_path: str | None = None) -> dict:
    # Real GM document_list contract: `source_path` is ALWAYS present
    # (None if absent — core/graph.py homogeneous-contract note).
    entry: dict[str, Any] = {"filename": filename, "source_path": source_path}
    if doc_id is not None:
        entry["id"] = doc_id
    return entry


def _doc_list(docs: list[dict]) -> dict:
    return {"status": "ok", "memory_id": "mem-1", "documents": docs}


# ─────────────────────────────────────────────────────────────
# 1. push() deletes are document_id-keyed, fail-closed without id
# ─────────────────────────────────────────────────────────────


async def test_reingest_delete_uses_document_id_from_document_list() -> None:
    """Delete-before-reingest resolves the GM document_id — never filename."""
    storage = FakeStorage(bank={"systemPatterns.md": "patterns"})
    await storage.put_json(_META, _explicit_meta(bank_mirror=["systemPatterns.md"]))
    bridge, factory = _bridge(
        responses={
            "document_list": _doc_list([_doc("systemPatterns.md", "uuid-1")])
        }
    )

    result = await _run(
        lambda: bridge.push(_SPACE), storage, _settings(long_embedded_url="")
    )

    assert result["status"] == "ok"
    assert result["deleted_before_reingest"] == 1
    assert result["errors"] == 0
    deletes = factory.instances[-1].args_for("document_delete")
    assert deletes == [{"memory_id": "mem-1", "document_id": "uuid-1"}]


async def test_orphan_clean_uses_document_id_from_document_list() -> None:
    """Ledger-scoped orphan cleanup resolves the GM document_id too."""
    storage = FakeStorage(bank={"systemPatterns.md": "patterns"})
    await storage.put_json(
        _META, _explicit_meta(bank_mirror=["systemPatterns.md", "stale.md"])
    )
    bridge, factory = _bridge(
        responses={
            "document_list": _doc_list(
                [
                    _doc("systemPatterns.md", "uuid-1"),
                    _doc("stale.md", "uuid-stale"),
                ]
            )
        }
    )

    result = await _run(
        lambda: bridge.push(_SPACE), storage, _settings(long_embedded_url="")
    )

    assert result["cleaned_orphans"] == 1
    deletes = factory.instances[-1].args_for("document_delete")
    assert {"memory_id": "mem-1", "document_id": "uuid-stale"} in deletes
    assert all("filename" not in a for a in deletes)


async def test_no_resolvable_id_skips_delete_fail_closed_and_still_ingests() -> None:
    """A GM doc without a resolvable id is NEVER deleted (no destructive call
    without positive identity evidence) — the re-ingest still proceeds and the
    push stays green (duplication is visible/repairable; a blind delete is not)."""
    storage = FakeStorage(bank={"systemPatterns.md": "patterns"})
    await storage.put_json(
        _META, _explicit_meta(bank_mirror=["systemPatterns.md", "stale.md"])
    )
    bridge, factory = _bridge(
        responses={
            "document_list": _doc_list(
                [
                    _doc("systemPatterns.md", None),  # no id -> no delete
                    _doc("stale.md", None),  # ledger orphan, no id -> kept
                ]
            )
        }
    )

    result = await _run(
        lambda: bridge.push(_SPACE), storage, _settings(long_embedded_url="")
    )

    inst = factory.instances[-1]
    assert inst.args_for("document_delete") == []
    assert result["status"] == "ok"
    assert result["pushed"] == 1
    assert result["deleted_before_reingest"] == 0
    assert result["cleaned_orphans"] == 0
    assert result["errors"] == 0


async def test_duplicate_mirror_copies_are_all_replaced() -> None:
    """Multiple GM ids for the SAME bank filename (duplicates inherited from
    the filename-keyed delete bug: every failed delete + re-ingest stacked a
    copy) are ALL deleted before the re-ingest — the push self-heals."""
    storage = FakeStorage(bank={"systemPatterns.md": "patterns"})
    await storage.put_json(_META, _explicit_meta(bank_mirror=["systemPatterns.md"]))
    bridge, factory = _bridge(
        responses={
            "document_list": _doc_list(
                [
                    _doc("systemPatterns.md", "uuid-old-1"),
                    _doc("systemPatterns.md", "uuid-old-2"),
                ]
            )
        }
    )

    result = await _run(
        lambda: bridge.push(_SPACE), storage, _settings(long_embedded_url="")
    )

    deletes = factory.instances[-1].args_for("document_delete")
    assert sorted(a["document_id"] for a in deletes) == ["uuid-old-1", "uuid-old-2"]
    assert result["deleted_before_reingest"] == 2
    assert result["pushed"] == 1


async def test_reingest_delete_never_touches_canonical_sharing_bank_filename() -> None:
    """A canonical P4-7 document (``source_path`` set) that happens to share
    a bank filename is NEVER deleted by the delete-before-reingest pass —
    only the mirror copy (``source_path`` None) is replaced. GM's real
    ``document_list`` exposes ``source_path`` (always present, None if
    absent), so the delete map is built exclusively from nul-source_path
    docs (Codex round-1 HIGH: data-loss path)."""
    storage = FakeStorage(bank={"systemPatterns.md": "patterns"})
    await storage.put_json(_META, _explicit_meta(bank_mirror=["systemPatterns.md"]))
    bridge, factory = _bridge(
        responses={
            "document_list": _doc_list(
                [
                    _doc("systemPatterns.md", "uuid-mirror", None),
                    _doc(
                        "systemPatterns.md",
                        "uuid-canonical",
                        "repo/DESIGN/systemPatterns.md",
                    ),
                ]
            )
        }
    )

    result = await _run(
        lambda: bridge.push(_SPACE), storage, _settings(long_embedded_url="")
    )

    deletes = factory.instances[-1].args_for("document_delete")
    assert deletes == [{"memory_id": "mem-1", "document_id": "uuid-mirror"}], (
        "push must delete ONLY the mirror copy; the canonical doc sharing "
        "the bank filename must never be a delete candidate"
    )
    assert result["deleted_before_reingest"] == 1
    assert result["pushed"] == 1


async def test_orphan_clean_never_touches_canonical_sharing_filename() -> None:
    """Ledger-scoped orphan cleanup is ALSO restricted to nul-source_path
    mirror copies: a canonical doc sharing the orphan filename survives."""
    storage = FakeStorage(bank={"systemPatterns.md": "patterns"})
    await storage.put_json(
        _META, _explicit_meta(bank_mirror=["systemPatterns.md", "stale.md"])
    )
    bridge, factory = _bridge(
        responses={
            "document_list": _doc_list(
                [
                    _doc("systemPatterns.md", "uuid-1", None),
                    _doc("stale.md", "uuid-stale-mirror", None),
                    _doc("stale.md", "uuid-stale-canonical", "repo/notes/stale.md"),
                ]
            )
        }
    )

    result = await _run(
        lambda: bridge.push(_SPACE), storage, _settings(long_embedded_url="")
    )

    deleted_ids = [
        a["document_id"] for a in factory.instances[-1].args_for("document_delete")
    ]
    assert "uuid-stale-canonical" not in deleted_ids
    assert "uuid-stale-mirror" in deleted_ids
    assert result["cleaned_orphans"] == 1


async def test_unresolved_orphan_is_kept_in_ledger_for_retry() -> None:
    """An orphan whose cleanup is skipped for lack of a resolvable mirror id
    STAYS in the rewritten ``bank_mirror`` ledger — it remains a cleanup
    candidate on the next push instead of silently leaving retry scope
    (Codex round-1 B2 weakness)."""
    storage = FakeStorage(bank={"systemPatterns.md": "patterns"})
    await storage.put_json(
        _META, _explicit_meta(bank_mirror=["systemPatterns.md", "stale.md"])
    )
    bridge, _factory = _bridge(
        responses={
            "document_list": _doc_list(
                [
                    _doc("systemPatterns.md", "uuid-1", None),
                    _doc("stale.md", None, None),  # mirror-shaped but no id
                ]
            )
        }
    )

    result = await _run(
        lambda: bridge.push(_SPACE), storage, _settings(long_embedded_url="")
    )

    assert result["cleaned_orphans"] == 0
    meta = json.loads(storage.objects[_META])
    assert meta["graph_memory"]["bank_mirror"] == [
        "stale.md",
        "systemPatterns.md",
    ], "the unresolved orphan must stay in the ledger for the next push"


def test_fake_transport_rejects_filename_keyed_delete() -> None:
    """The strict fake enforces the REAL GM contract: a filename-keyed
    document_delete (the pre-P7-8 bug shape) errors instead of succeeding —
    the guard suite is non-vacuous and the masking can never return."""
    fake = FakeGraphTransport()
    resolved = fake._resolve(
        "document_delete", {"memory_id": "m", "filename": "x.md"}
    )
    assert resolved["status"] == "error"
    assert "document_id" in resolved["message"]

    ok = fake._resolve(
        "document_delete", {"memory_id": "m", "document_id": "uuid-1"}
    )
    assert ok["status"] == "deleted"


# ─────────────────────────────────────────────────────────────
# 2. GM document_delete write-gate (AST — module not importable here)
# ─────────────────────────────────────────────────────────────


def _gm_function(tree: ast.Module, name: str) -> ast.AsyncFunctionDef:
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"GM server.py: async def {name} not found")


def _first_call_lineno(fn: ast.AsyncFunctionDef, callee: str) -> int:
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == callee
        ):
            return node.lineno
    raise AssertionError(f"document_delete: no call to {callee}()")


def test_gm_document_delete_is_write_gated_before_deletion() -> None:
    """GM's document_delete must gate check_memory_access THEN
    check_write_permission BEFORE invoking delete_document_everywhere.
    (AST on the vendored source: the GM server module pulls its middleware
    stack and is not importable in the Hivemind venv — same technique as the
    P7-4 guards.)"""
    tree = ast.parse(GM_SERVER.read_text(encoding="utf-8"))
    fn = _gm_function(tree, "document_delete")

    access_line = _first_call_lineno(fn, "check_memory_access")
    write_line = _first_call_lineno(fn, "check_write_permission")
    delete_line = _first_call_lineno(fn, "delete_document_everywhere")

    assert access_line < write_line < delete_line, (
        f"document_delete gate order broken: access@{access_line}, "
        f"write@{write_line}, delete@{delete_line}"
    )


# ─────────────────────────────────────────────────────────────
# 3. Internal token scope = EXACTLY {read, write}
# ─────────────────────────────────────────────────────────────


class _TokenStorage(FakeStorage):
    """Storage pre-seeded with an empty token store."""


@pytest.mark.parametrize(
    "perms",
    [
        ["read", "write", "manage"],
        ["read", "write", "admin"],
        ["admin"],
        ["manage"],
        ["read"],  # a weaker set is also rejected: the contract is EXACT
        ["read", "write", "read2"],
    ],
)
async def test_register_internal_long_token_rejects_non_exact_scope(perms) -> None:
    storage = _TokenStorage()
    with patch("live_mem.core.tokens.get_storage", return_value=storage):
        service = TokenService()
        result = await service.register_internal_long_token(
            "tok-internal", permissions=perms
        )
    assert result["status"] == "error"
    # And nothing was written to the store.
    assert all("tokens" not in k for k in storage.objects)


@pytest.mark.parametrize("perms", [None, ["read", "write"], ["write", "read"]])
async def test_register_internal_long_token_accepts_exact_read_write(perms) -> None:
    storage = _TokenStorage()
    with patch("live_mem.core.tokens.get_storage", return_value=storage):
        service = TokenService()
        result = await service.register_internal_long_token(
            "tok-internal", permissions=perms
        )
    assert result["status"] == "ok"


async def test_register_normalizes_drifted_reserved_entry_scope() -> None:
    """An EXISTING same-hash `internal-long` entry whose permissions drifted
    (e.g. registered wider before the exact-scope lock) is brought back to
    exactly {read, write} on the next registration — a widened internal
    scope can never stay live (Codex round-1 B5 residual)."""
    import hashlib as _hashlib

    raw = "tok-internal"
    drifted_hash = "sha256:" + _hashlib.sha256(raw.encode()).hexdigest()
    storage = _TokenStorage()
    await storage.put_json(
        "_system/tokens.json",
        {
            "version": 2,
            "tokens": [
                {
                    "hash": drifted_hash,
                    "name": "internal-long",
                    "permissions": ["read", "write", "admin"],
                    "space_ids": [],
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "expires_at": None,
                }
            ]
        },
    )

    with patch("live_mem.core.tokens.get_storage", return_value=storage):
        service = TokenService()
        result = await service.register_internal_long_token(raw)

    assert result["status"] == "ok"
    assert result["registered"] is False
    assert result["permissions_normalized"] == 1
    store = json.loads(storage.objects["_system/tokens.json"])
    (entry,) = [t for t in store["tokens"] if t["name"] == "internal-long"]
    assert sorted(entry["permissions"]) == ["read", "write"]


# ─────────────────────────────────────────────────────────────
# 4. Backups: sentinel-at-rest in RAW backup; no GM edge in restore
# ─────────────────────────────────────────────────────────────


async def test_raw_backup_of_embedded_space_carries_sentinel_never_live_token() -> None:
    """End-to-end: a real long_push auto-binds the space to the embedded
    runtime, then a RAW BackupService.create() snapshot (byte-for-byte S3
    copy — no masking) is scanned: the live internal token appears in NO
    backed-up object; the backup's _meta.json stores the sentinel."""
    storage = FakeStorage(bank={"systemPatterns.md": "patterns"})
    await storage.put_json(_META, {"space_id": _SPACE, "version": 1})
    bridge, _factory = _bridge()

    push_result = await _run(lambda: bridge.push(_SPACE), storage, _settings())
    assert push_result["status"] == "ok"

    # Raw snapshot via the real BackupService over the same storage.
    with patch("live_mem.core.backup.get_storage", return_value=storage):
        backup_result = await BackupService().create(_SPACE)
    assert backup_result["status"] == "created"

    backup_keys = [k for k in storage.objects if k.startswith("_backups/")]
    assert backup_keys, "backup created no objects"

    # The live embedded token is at rest NOWHERE in the backup.
    for key in backup_keys:
        assert _EMBEDDED_TOKEN not in storage.objects[key], (
            f"live embedded token leaked into raw backup object {key}"
        )

    # And the backed-up _meta.json carries the sentinel binding.
    meta_keys = [k for k in backup_keys if k.endswith("_meta.json")]
    assert meta_keys, "backup misses _meta.json"
    backup_meta = json.loads(storage.objects[meta_keys[0]])
    assert backup_meta["graph_memory"]["token"] == EMBEDDED_TOKEN_SENTINEL


def test_hivemind_backup_module_has_no_graph_memory_edge() -> None:
    """Structural guarantee for the ADR-0010 recovery boundary: restoring
    Hivemind protocol state can never consume long/graph state, because the
    backup/restore module has no Graph Memory client/bridge import at all."""
    tree = ast.parse(
        (REPO_ROOT / "src" / "live_mem" / "core" / "backup.py").read_text(
            encoding="utf-8"
        )
    )
    forbidden = ("graph_bridge", "long_engine", "GraphMemoryClient", "mcp_memory")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""] + [a.name for a in node.names]
        else:
            continue
        for name in names:
            assert not any(tok in name for tok in forbidden), (
                f"backup.py imports a Graph Memory edge: {name} "
                f"(Hivemind recovery must never consume long state, ADR-0010)"
            )


# ─────────────────────────────────────────────────────────────
# 5. SECURITY.md documents the embedded trust boundary & GM backups
# ─────────────────────────────────────────────────────────────

_GM_BACKUP_TOOLS = (
    "backup_create",
    "backup_list",
    "backup_restore",
    "backup_download",
    "backup_delete",
    "backup_restore_archive",
)


def _security_doc() -> str:
    return (REPO_ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")


def test_security_doc_lists_embedded_services_and_boundary() -> None:
    doc = _security_doc()
    assert "### 2.1 Embedded internal services" in doc
    for service in ("graph-memory", "neo4j", "qdrant"):
        assert f"`{service}`" in doc, f"SECURITY.md must list `{service}`"
    assert "internal token" in doc


def test_security_doc_documents_internal_token_handling() -> None:
    doc = _security_doc()
    assert "### 4.5 Embedded long runtime (ADR-0019)" in doc
    assert "`internal-long`" in doc
    assert "`__embedded__`" in doc
    assert "exactly `read` + `write`" in doc
    assert "never `manage`" in doc
    assert "never `admin`" in doc


def test_security_doc_declares_gm_backups_long_runtime_only() -> None:
    doc = _security_doc()
    assert "### 4.6 Graph Memory-native backups" in doc
    assert "long-runtime backup only" in doc
    assert "never Hivemind protocol\n  recovery truth" in doc.replace("**", "")
    for tool in _GM_BACKUP_TOOLS:
        assert f"`{tool}`" in doc, (
            f"SECURITY.md §4.6 must name the GM-native backup tool `{tool}`"
        )
