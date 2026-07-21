# -*- coding: utf-8 -*-
"""
Pytest bootstrap shared by the whole Hivemind suite.

P7-4 (#120) needs to unit-test the vendored embedded Graph Memory auth surface
(`services/graph-memory/src/mcp_memory/...`). That tree is NOT an installed
package — Hivemind's `pyproject` only exposes `src/` (the `live_mem` package) —
so a plain `import mcp_memory...` from `tests/` would not resolve and every P7-4
lock would silently fail to collect.

This conftest puts the vendored GM `src/` on `sys.path` so the **import-light**
GM modules (`mcp_memory.auth.context`, `mcp_memory.auth.s3_token_validator`,
`mcp_memory.core.validators`) are importable from the Hivemind test venv.

IMPORTANT: the Hivemind test venv ships `boto3` but NOT `neo4j` / `qdrant_client`.
Tests must therefore only import the import-light GM modules above (the
`s3_token_validator` lazy-imports boto3/config inside methods). Heavy modules
(`mcp_memory.auth.middleware`, `mcp_memory.auth.token_manager`) pull `neo4j`
and must be asserted via source inspection, never imported here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_GM_SRC = _REPO_ROOT / "services" / "graph-memory" / "src"

# Prepend so the vendored GM `mcp_memory` package resolves before anything else.
# Guarded so repeated collection does not stack duplicates.
_gm_src_str = str(_GM_SRC)
if _GM_SRC.is_dir() and _gm_src_str not in sys.path:
    sys.path.insert(0, _gm_src_str)
