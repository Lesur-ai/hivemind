# -*- coding: utf-8 -*-
"""P13-1A..1D (#274-#277) — package boundary and frozen public surface.

The shared inference package is a pure provider boundary. This locks, at the
package level:

- it imports neither ``live_mem`` nor ``mcp_memory`` (execution-level, in a
  fresh interpreter, complementing the AST scan in the config suite);
- importing it stays import-light — no ``httpx`` or provider SDK is pulled in
  until a transport/adapter seam is actually used, INCLUDING through the #276
  runtime holder, whose registry/adapter imports are lazy per factory;
- the exported surface is exactly the frozen foundation/runtime set plus the
  pure #277 Qdrant collection-identity primitives;
- no normalized record carries a protocol-authority field;
- the module set is the frozen public foundation/runtime floor plus the
  certification budget guard used by the public adapter and an all-or-none
  private provider-certification overlay — nothing else is smuggled in here.

The registered ``adapters`` subpackage (#275) gets its own import-lightness
and module-set boundary checks below, mirroring the top-level ones: it must
not pull in ``httpx``/``openai`` merely by being imported, and its module set
is exactly the three registered adapter modules.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import subprocess
import sys

import pytest

import hivemind_inference
from hivemind_inference import (
    ChatRequest,
    ChatResult,
    EmbeddingRequest,
    EmbeddingResult,
    ProbeResult,
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PKG_DIR = _REPO_ROOT / "src" / "hivemind_inference"
_SRC_DIR = _REPO_ROOT / "src"

EXPECTED_PUBLIC_SURFACE = {
    "CHAT_PROVIDER_IDS",
    "ChatMessage",
    "ChatRequest",
    "ChatResult",
    "EMBEDDING_COLLECTION_IDENTITY_FIELDS",
    "EMBEDDING_COLLECTION_SCHEMA_VERSION",
    "EMBEDDING_CONTRACT_VERSION",
    "EMBEDDING_PROVIDER_IDS",
    "ERROR_CATEGORIES",
    "EmbeddingCollectionIdentity",
    "EmbeddingIdentityError",
    "EmbeddingRequest",
    "EmbeddingResult",
    "InferenceConfig",
    "InferenceConfigError",
    "InferenceError",
    "InferenceRoleUnavailable",
    "InferenceRuntime",
    "PROVIDER_TO_ADAPTER",
    "ProbeResult",
    "ResolvedChatProfile",
    "ResolvedEmbeddingProfile",
    "adapter_for_provider",
    "build_configured_embedding_collection_identity",
    "build_embedding_collection_identity",
    "canonical_qdrant_collection_name",
    "embedding_metadata_fingerprint",
    "embedding_profile_fingerprint",
    "endpoint_sha256",
    "merged_environment",
    "parse_embedding_collection_identity",
    "resolve_inference_config",
    "validate_embedding_collection_identity",
}

# The foundation slice modules (#274), the shared process-lifecycle guard
# (#306), and the consumer runtime plus its holder (#276). Adapters (#275) live
# in their own subpackage, asserted separately below.
#
# `finalize.py` is deliberately ABSENT: #276 carried a `run_finalizers` of its
# own until #306 landed a strictly wider one in `asgi_lifespan.py` (it also
# recognises a cancellation nested inside an `ExceptionGroup`). Two exhaustive
# finalisers is exactly the divergence the shared guard exists to end.
EXPECTED_PUBLIC_MODULES = {
    "__init__.py",
    "asgi_lifespan.py",
    "certification_budget.py",
    "collection_identity.py",
    "config.py",
    "egress.py",
    "errors.py",
    "holder.py",
    "process_window.py",
    "profiles.py",
    "records.py",
    "registry.py",
    "retry.py",
    "runtime.py",
}
PRIVATE_OVERLAY_MODULES = {
    "certification.py",
    "live_verification.py",
    "protected_certification.py",
    "reference_profiles.py",
}


def _isolated_modules_after_module_import(*module_names: str) -> set[str]:
    """Import the named modules in a FRESH interpreter and report which of the
    sensitive modules ended up loaded."""
    script = "import sys\n"
    for module_name in module_names:
        script += f"import {module_name}\n"
    script += (
        "watch = ('live_mem', 'mcp_memory', 'httpx', 'openai', 'neo4j', 'qdrant_client')\n"
        "print(','.join(m for m in watch if m in sys.modules))\n"
    )
    env = dict(os.environ)
    # Ensure the package resolves from src/ without relying on the editable
    # install; importable-but-not-imported is exactly what we assert for httpx.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_SRC_DIR), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"import failed: {result.stderr}"
    loaded = result.stdout.strip()
    return set(loaded.split(",")) if loaded else set()


def _isolated_modules_after_import() -> set[str]:
    """Import the package in a FRESH interpreter and report which of the
    sensitive modules ended up loaded."""
    return _isolated_modules_after_module_import("hivemind_inference")


def _isolated_modules_after_adapters_import() -> set[str]:
    """Import every registered adapter module in a FRESH interpreter and
    report which sensitive modules ended up loaded.

    The registered adapter modules import ``httpx``/``openai`` lazily inside
    their constructors/methods (never at module top level), so merely
    importing them — without constructing an adapter — must stay as
    import-light as the parent package. ``adapters/__init__.py`` is a bare
    docstring with no submodule imports, so a plain
    ``import hivemind_inference.adapters`` alone would never actually load
    ``common``/``openai_compatible``/``anthropic_native`` and would pass
    vacuously even if one of them imported ``httpx``/``openai`` at module top
    level; every concrete module is imported by name to actually exercise it.
    """
    return _isolated_modules_after_module_import(
        "hivemind_inference.adapters",
        "hivemind_inference.adapters.common",
        "hivemind_inference.adapters.openai_compatible",
        "hivemind_inference.adapters.anthropic_native",
    )


class TestPackageBoundary:
    def test_import_does_not_pull_consumers(self):
        loaded = _isolated_modules_after_import()
        assert "live_mem" not in loaded
        assert "mcp_memory" not in loaded

    def test_import_is_light_no_httpx_or_sdk(self):
        loaded = _isolated_modules_after_import()
        # httpx / provider SDK are imported lazily inside the transport and
        # adapter seams, never at package import time.
        assert "httpx" not in loaded
        assert "openai" not in loaded
        assert "neo4j" not in loaded
        assert "qdrant_client" not in loaded


class TestFrozenPublicSurface:
    def test_all_matches_frozen_foundation_surface(self):
        assert set(hivemind_inference.__all__) == EXPECTED_PUBLIC_SURFACE

    def test_all_has_no_duplicates(self):
        assert len(hivemind_inference.__all__) == len(set(hivemind_inference.__all__))

    def test_every_exported_name_is_resolvable(self):
        for name in hivemind_inference.__all__:
            assert hasattr(hivemind_inference, name), name

    def test_no_deferred_symbols_leaked(self):
        # build_chat_provider/build_embedding_provider exist (#275) but only in
        # hivemind_inference.registry/adapters: consumers reach an adapter
        # exclusively through the #276 runtime holder, which owns its transport
        # and closes it on shutdown. Re-exporting the raw factories at the
        # package surface would advertise a construction path with no owner.
        for deferred in (
            "build_chat_provider",
            "build_embedding_provider",
            "build_embedding_collection_metadata",
        ):
            assert deferred not in hivemind_inference.__all__


class TestModuleSetBoundary:
    def test_module_set_is_the_foundation_slice(self):
        actual = {p.name for p in _PKG_DIR.glob("*.py")}
        private_overlay = actual & PRIVATE_OVERLAY_MODULES
        assert private_overlay in (set(), PRIVATE_OVERLAY_MODULES)
        assert actual == EXPECTED_PUBLIC_MODULES | private_overlay

    def test_adapters_subpackage_module_set_is_exactly_registered(self):
        # #275: the adapters subpackage exists with exactly the registered
        # adapter modules — no stray/experimental module smuggled in.
        adapters_dir = _PKG_DIR / "adapters"
        assert adapters_dir.is_dir()
        actual = {p.name for p in adapters_dir.glob("*.py")}
        assert actual == {
            "__init__.py",
            "common.py",
            "openai_compatible.py",
            "anthropic_native.py",
        }

    def test_runtime_module_stays_import_light(self):
        # #276: the runtime holder is the ONLY consumer-facing construction
        # seam, so it is the module most likely to pull the adapter/SDK stack
        # in at import time. Importing it by name (not just the package) must
        # still load no transport: every registry import lives inside a
        # factory method.
        loaded = _isolated_modules_after_module_import("hivemind_inference.runtime")
        assert "httpx" not in loaded
        assert "openai" not in loaded
        assert "live_mem" not in loaded
        assert "mcp_memory" not in loaded


class TestAdaptersPackageBoundary:
    """#275: the registered adapters subpackage mirrors the parent package's
    import-lightness discipline — provider SDKs are constructed lazily inside
    the adapter classes, never pulled in merely by importing the subpackage."""

    def test_import_does_not_pull_consumers(self):
        loaded = _isolated_modules_after_adapters_import()
        assert "live_mem" not in loaded
        assert "mcp_memory" not in loaded

    def test_import_is_light_no_httpx_or_sdk(self):
        loaded = _isolated_modules_after_adapters_import()
        assert "httpx" not in loaded
        assert "openai" not in loaded
        assert "neo4j" not in loaded
        assert "qdrant_client" not in loaded


class TestRecordsCarryNoAuthority:
    FORBIDDEN = {
        "space_id",
        "commit_id",
        "bank_version",
        "term",
        "membership",
        "watermark",
        "tombstone",
        "manifest",
        "lease",
        "fencing",
        "staging",
        "queue",
    }

    @pytest.mark.parametrize(
        "record_type",
        [ChatRequest, ChatResult, EmbeddingRequest, EmbeddingResult, ProbeResult],
    )
    def test_no_protocol_authority_fields(self, record_type):
        names = {f.name for f in dataclasses.fields(record_type)}
        assert not (names & self.FORBIDDEN)
