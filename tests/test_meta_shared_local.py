# -*- coding: utf-8 -*-
"""
P4-3 (EPIC #6) — lock the ``_meta`` shared/local contract for the long tier.

The shared-vs-local split (``SHARED_META_FIELDS`` / ``meta_shared_projection`` /
``meta_local_complement`` / ``mask_meta_secrets``) was delivered by PR #17 and
ratified by ADR-0012. P4-3 does NOT re-implement it; it LOCKS the long-tier
contract that P4-5 (watermark) and P5 (mesh) depend on:

- the entire ``graph_memory`` block (url / token / memory_id / push metrics) is
  local-only and absent from the shared projection;
- the derived-watermark coords (``bank_version`` / ``commit_id`` / ``term`` /
  ``provenance``), recorded INSIDE the ``graph_memory`` block by P4-5, inherit
  that locality (no ``SHARED_META_FIELDS`` change);
- ``mask_meta_secrets`` masks ``graph_memory.token`` and is wired into the
  outward paths (``space.py`` summary/export, ``backup.py`` download);
- ``local ∪ shared`` losslessly partitions the document (deny-by-default);
- non-Hivemind ``_meta`` (no ``graph_memory`` block) round-trips unchanged.

Offline — only the pure ``live_mem.core.models`` module; no storage / network /
S3 / LLM.
"""

from __future__ import annotations

from pathlib import Path

from live_mem.core.models import (
    SHARED_META_FIELDS,
    mask_meta_secrets,
    meta_local_complement,
    meta_shared_projection,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "live_mem"
_WATERMARK_COORDS = ("bank_version", "commit_id", "term", "provenance")


def _full_meta() -> dict:
    return {
        "space_id": "demo",
        "description": "d",
        "owner": "o",
        "created_at": "2026-01-01T00:00:00Z",
        "last_consolidation": "2026-01-02T00:00:00Z",
        "consolidation_count": 3,
        "total_notes_processed": 42,
        "version": "2.5.2",
        "graph_memory": {
            "url": "https://gm.example.com/mcp",
            "token": "supersecrettoken-abc123",
            "memory_id": "mem-1",
            "ontology": "general",
            "push_count": 7,
            "files_pushed": 12,
            "last_push": "2026-01-03T00:00:00Z",
            # P4-5 derived watermark, nested inside the local block:
            "bank_version": 9,
            "commit_id": "c-deadbeef",
            "term": 4,
            "provenance": "mid-consolidation",
        },
    }


# --------------------------------------------------------------------------- #
# graph_memory block (+ nested watermark) is local-only                        #
# --------------------------------------------------------------------------- #


def test_graph_memory_not_in_allowlist():
    assert "graph_memory" not in SHARED_META_FIELDS


def test_shared_projection_excludes_graph_memory_block():
    assert "graph_memory" not in meta_shared_projection(_full_meta())


def test_shared_projection_leaks_no_secret_or_watermark_coord():
    shared = meta_shared_projection(_full_meta())
    flat = repr(shared)
    for coord in _WATERMARK_COORDS:
        assert coord not in shared, coord
        assert coord not in flat, coord
    for secret in ("supersecrettoken", "gm.example.com", "mem-1"):
        assert secret not in flat, secret


def test_shared_projection_only_allowlisted_keys():
    assert set(meta_shared_projection(_full_meta())).issubset(set(SHARED_META_FIELDS))


def test_watermark_coords_live_in_local_complement():
    gm = meta_local_complement(_full_meta())["graph_memory"]
    for coord in _WATERMARK_COORDS:
        assert coord in gm, coord


# --------------------------------------------------------------------------- #
# local ∪ shared partition (deny-by-default)                                   #
# --------------------------------------------------------------------------- #


def test_local_union_shared_reconstructs_full_meta():
    m = _full_meta()
    assert {**meta_local_complement(m), **meta_shared_projection(m)} == m


def test_unknown_future_field_falls_local_not_shared():
    m = _full_meta()
    m["some_future_field"] = {"x": 1}
    assert "some_future_field" not in meta_shared_projection(m)
    assert "some_future_field" in meta_local_complement(m)


# --------------------------------------------------------------------------- #
# mask_meta_secrets on outward paths                                           #
# --------------------------------------------------------------------------- #


def test_mask_masks_graph_memory_token_without_mutating_input():
    m = _full_meta()
    masked = mask_meta_secrets(m)
    assert masked["graph_memory"]["token"] == "supersec..."  # token[:8] + "..."
    assert m["graph_memory"]["token"] == "supersecrettoken-abc123"  # input untouched


def test_mask_short_token_uses_stars():
    assert mask_meta_secrets({"graph_memory": {"token": "short"}})["graph_memory"]["token"] == "***"


def test_mask_wired_into_space_and_backup_outward_paths():
    space_src = (_SRC / "core" / "space.py").read_text(encoding="utf-8")
    backup_src = (_SRC / "core" / "backup.py").read_text(encoding="utf-8")
    assert "mask_meta_secrets" in space_src, "space_summary/space_export must mask"
    assert "mask_meta_secrets" in backup_src, "backup_download must mask"


# --------------------------------------------------------------------------- #
# non-Hivemind / no graph_memory block                                         #
# --------------------------------------------------------------------------- #


def test_non_hivemind_meta_roundtrips_unchanged():
    m = {k: v for k, v in _full_meta().items() if k != "graph_memory"}
    assert {**meta_local_complement(m), **meta_shared_projection(m)} == m
    assert mask_meta_secrets(m) == m  # no-op without a graph_memory block


def test_meta_without_graph_memory_block_projects_cleanly():
    m = {"space_id": "x", "version": "1"}
    assert meta_shared_projection(m) == {"space_id": "x", "version": "1"}
    assert meta_local_complement(m) == {}
