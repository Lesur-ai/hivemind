# -*- coding: utf-8 -*-
"""
P4-9 (EPIC #6) — long/commit isolation guard, **static subset** (Wave 0).

P4-9 is the anti-complacent gate that locks the central P4 invariant: the long
tier is **protocol-derived, never authoritative** — it is never on the
commit / rollback / audit / tombstone / recovery path, and no `BANK_COMMIT`
auto-triggers a graph push (ADR-0010, refined by ADR-0017).

This module is the part of that gate that can run **today**, against `main`
before any P4 code lands — pure structural / negative-import assertions plus the
already-shipped shared-metadata exclusion:

  * No commit / consolidation / hive-state module imports the long or graph
    engine (negative-import, by AST — so a variable named ``long_v`` cannot
    create a false positive).
  * The consolidator source contains no graph-push call (grep-level
    anti-complacency: consolidation must never push to the graph).
  * The `long` engine does not import the commit path (reverse direction —
    ADR-0010 "no back-edge", encoded structurally).
  * The entire ``graph_memory`` block (and any derived-watermark fields nested
    inside it) is excluded from the shared `_meta` projection (ADR-0012).
  * The `mid` engine documents that ``long_*`` / ``graph_push`` is never a
    `WriteSink` write.

DEFERRED to P4-4 (needs ``tests/fakes/fake_graph_transport.py``), NOT covered
here — the *runtime* half of the P4-9 gate:
  * run a real consolidation against a connected space and assert **zero**
    transport calls;
  * run a fake `BANK_COMMIT` apply and assert it triggers **zero** pushes;
  * inject a transport error during a fake commit and assert the commit still
    succeeds (a long-engine failure never blocks a mid commit);
  * assert the derived watermark never appears in the shared projection (the
    watermark fields themselves arrive with P4-5).

Pure stdlib (``ast`` / ``pathlib``) for the negative-import assertions; the
projection assertion imports only ``live_mem.core.models`` (pydantic, no
network / S3 / LLM). Repo root resolved from ``__file__``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src" / "live_mem"

# Modules on the commit / consolidation / hive-state path that must stay
# structurally blind to the long/graph tier.
_COMMIT_PATH_MODULES = (
    _SRC / "core" / "consolidator.py",
    _SRC / "core" / "write_sink.py",
    _SRC / "core" / "hivemind" / "state.py",
    _SRC / "core" / "hivemind" / "lifecycle.py",
)

# Import tokens that must never appear in a commit-path module's imports.
_FORBIDDEN_LONG_IMPORTS = (
    "graph_bridge",
    "graph_memory",
    "long_engine",
    "engines.long",
    "tools.graph",
    "GraphMemoryClient",
    "GraphBridgeService",
)

# Import tokens the `long` engine must never reach for (reverse direction).
_FORBIDDEN_COMMIT_IMPORTS = (
    "consolidator",
    "write_sink",
    "engines.mid",
    "hivemind.state",
    "hivemind.lifecycle",
    "assert_commit",
)


def _read(path: Path) -> str:
    assert path.exists(), f"expected source file missing: {path}"
    return path.read_text(encoding="utf-8")


def _imported_dotted_names(path: Path) -> set[str]:
    """Every dotted module/name an ``import`` / ``from ... import`` references.

    Relative imports are rendered with their leading dots (e.g. ``.storage``,
    ``..config``) so a token like ``graph_bridge`` matches ``.graph_bridge``.
    Only real import statements are inspected — identifiers, attributes and
    comments are ignored (that is the whole point of using AST, not grep).
    """
    tree = ast.parse(_read(path), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = ("." * (node.level or 0)) + (node.module or "")
            names.add(base)
            for alias in node.names:
                names.add(f"{base}.{alias.name}")
    return names


def _forbidden_hits(names: set[str], tokens) -> list[str]:
    return sorted({n for n in names for tok in tokens if tok in n})


# --------------------------------------------------------------------------- #
# Negative-import: the commit/consolidation path never imports long/graph      #
# --------------------------------------------------------------------------- #


def test_commit_path_modules_do_not_import_long_or_graph():
    for module in _COMMIT_PATH_MODULES:
        names = _imported_dotted_names(module)
        hits = _forbidden_hits(names, _FORBIDDEN_LONG_IMPORTS)
        assert not hits, (
            f"{module.relative_to(_REPO_ROOT)} imports the long/graph tier "
            f"(commit path must stay blind to it): {hits}"
        )


def test_consolidator_source_has_no_graph_push_call():
    """Anti-complacency: even beyond imports, the consolidator body must contain
    no graph-bridge / graph-push reference (consolidation never pushes)."""
    body = _read(_SRC / "core" / "consolidator.py")
    for needle in ("graph_bridge", "graph_push", "GraphBridgeService", "GraphMemoryClient"):
        assert needle not in body, (
            f"consolidator.py references {needle!r} — consolidation must never push to graph"
        )


# --------------------------------------------------------------------------- #
# Reverse direction: the long engine never imports the commit path             #
# (ADR-0010 "no back-edge", encoded structurally)                              #
# --------------------------------------------------------------------------- #


def test_long_engine_does_not_import_commit_path():
    long_engine = _SRC / "core" / "engines" / "long_engine.py"
    names = _imported_dotted_names(long_engine)
    hits = _forbidden_hits(names, _FORBIDDEN_COMMIT_IMPORTS)
    assert not hits, (
        f"engines/long_engine.py imports the commit path (no back-edge violated): {hits}"
    )


# --------------------------------------------------------------------------- #
# graph_memory block (+ nested watermark) excluded from the shared projection  #
# --------------------------------------------------------------------------- #


def test_graph_memory_block_excluded_from_shared_meta_projection():
    from live_mem.core import models

    assert "graph_memory" not in models.SHARED_META_FIELDS, (
        "graph_memory must not be in the shared allowlist (it is local-only, ADR-0012)"
    )

    meta = {
        "space_id": "demo",
        "description": "d",
        "version": "1",
        "graph_memory": {
            "url": "https://gm.example",
            "token": "secret",
            "push_count": 3,
            # P4-5 derived watermark nested inside the local block:
            "bank_version": 7,
            "commit_id": "c-123",
            "term": 2,
            "provenance": "mid-consolidation",
        },
    }

    shared = models.meta_shared_projection(meta)
    assert "graph_memory" not in shared, (
        "graph_memory leaked into the shared projection"
    )
    # No watermark coordinate may surface in the shared projection either.
    for coord in ("bank_version", "commit_id", "term", "provenance", "token", "url"):
        assert coord not in shared, f"{coord!r} leaked into the shared projection"
    assert shared.get("space_id") == "demo", "shared projection must keep allowlisted fields"

    local = models.meta_local_complement(meta)
    assert "graph_memory" in local, "graph_memory must live in the local complement"
    # Round-trip identity: local complement ∪ shared projection == original meta.
    assert {**local, **shared} == meta, "local ∪ shared must reconstruct the full meta"


# --------------------------------------------------------------------------- #
# mid engine documents the long-is-never-a-WriteSink-write doctrine            #
# --------------------------------------------------------------------------- #


def test_mid_engine_documents_long_is_never_a_writesink_write():
    body = _read(_SRC / "core" / "engines" / "mid.py")
    low = body.lower()
    # Case-insensitive so a benign reword/recasing of the doctrine prose does not
    # flip RED while the invariant still holds.
    assert "graph_push" in low and "never a writesink write" in low, (
        "mid.py must document that long_*/graph_push is never a WriteSink write"
    )
    assert "ADR-0010" in body, "mid.py must cite ADR-0010 for the long-is-downstream doctrine"
    # And mid.py itself must not import the long/graph tier.
    hits = _forbidden_hits(_imported_dotted_names(_SRC / "core" / "engines" / "mid.py"),
                           _FORBIDDEN_LONG_IMPORTS)
    assert not hits, f"mid.py imports the long/graph tier: {hits}"
