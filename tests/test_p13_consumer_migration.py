# -*- coding: utf-8 -*-
"""P13-1C (#276) — consumer migration, health contract, and deployment purity.

One suite per acceptance criterion of the lot, each written so it fails if the
criterion regresses rather than merely if an implementation detail moves:

1. **No direct provider SDK construction remains outside registered adapters.**
   An AST sweep of BOTH consumer trees, plus an import sweep, so a future edit
   cannot reintroduce a client under a different name or module.
2. **Core and Graph Memory resolve the same immutable role profiles.** The two
   singletons are resolved from ONE environment and compared field by field.
3. **Health performs zero chat generation and zero embedding, and distinguishes
   unsupported discovery from failure.** Proven against the real registered
   adapter and the deterministic emulator.
4. **Historical health fields remain byte-compatible and the new role fields are
   additive and correctly redacted.** The historical shapes are pinned exactly;
   the public/authenticated split is pinned as an exact key set.
5. **Split and legacy deployments start without Compose injecting the opposite
   configuration family.**
6. **The embedded image installs the exact shared package** (repository-root
   build context, wired identically in Compose and the public CI overlay).

Offline: no network, no S3, no Neo4j, no Qdrant. Provider traffic goes to the
in-process ``InferenceEmulator``.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
import yaml

from hivemind_inference import InferenceConfigError
from hivemind_inference.runtime import InferenceRuntimeClosed
from tests.fakes.inference_emulator import InferenceEmulator
from tests.fakes.inference_fakes import (
    apply_graph_memory_baseline_env,
    core_inference_runtime,
    make_chat_profile,
    make_embedding_profile,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CORE_PKG = _REPO_ROOT / "src" / "live_mem"
_GM_PKG = _REPO_ROOT / "services" / "graph-memory" / "src" / "mcp_memory"
_SHARED_PKG = _REPO_ROOT / "src" / "hivemind_inference"

_EXTERNAL_ORIGIN = "http://llm.p13-1c.invalid/v1"


def _module_trees(package: Path):
    for path in sorted(package.rglob("*.py")):
        yield path.relative_to(_REPO_ROOT).as_posix(), ast.parse(
            path.read_text(encoding="utf-8")
        )


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _registered_hooks(factory: ast.AST) -> dict[str, list[str]]:
    """The exact source form of every lifecycle contract field, in order.

    Rendered with ``ast.unparse`` rather than reduced to bare names: the
    ownership contract this pins is *how* each hook is registered — guarded or
    not, and in which position — so a check that only collected names would
    pass on an unguarded rewrite (R7-F1).
    """
    hooks = next(
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "LifespanHooks"
    )
    registered: dict[str, list[str]] = {}
    for keyword in hooks.keywords:
        if keyword.arg is None:
            continue
        if isinstance(keyword.value, (ast.Tuple, ast.List)):
            registered[keyword.arg] = [
                ast.unparse(element) for element in keyword.value.elts
            ]
        else:
            registered[keyword.arg] = [ast.unparse(keyword.value)]
    return registered


def _imported_roots(tree: ast.AST) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".", 1)[0])
    return roots


# --------------------------------------------------------------------------- #
# 1. Provider SDK construction is confined to the registered adapters          #
# --------------------------------------------------------------------------- #

_SDK_CONSTRUCTORS = {"AsyncOpenAI", "OpenAI", "AsyncAnthropic", "Anthropic"}
_SDK_MODULES = {"openai", "anthropic", "tenacity"}


class TestNoDirectProviderSdkConstruction:
    @pytest.mark.parametrize("package", [_CORE_PKG, _GM_PKG], ids=["core", "graph"])
    def test_no_consumer_module_constructs_a_provider_client(self, package):
        offenders = [
            rel
            for rel, tree in _module_trees(package)
            if _called_names(tree) & _SDK_CONSTRUCTORS
        ]
        assert offenders == []

    @pytest.mark.parametrize("package", [_CORE_PKG, _GM_PKG], ids=["core", "graph"])
    def test_no_consumer_module_imports_a_provider_sdk_or_its_retry_layer(
        self, package
    ):
        """The retry layer is in scope with the SDKs on purpose: ADR-0027 puts
        the single bounded retry inside the adapters, so a consumer-side
        ``tenacity`` decorator would silently multiply paid provider requests
        whose delivery is ambiguous."""
        offenders = [
            rel
            for rel, tree in _module_trees(package)
            if _imported_roots(tree) & _SDK_MODULES
        ]
        assert offenders == []

    def test_the_registered_adapters_are_the_only_construction_seam(self):
        """Non-vacuity: the guards above would also pass on a repository that
        simply has no provider integration. The shared package must still be
        the place where a transport IS built."""
        builders = [
            rel
            for rel, tree in _module_trees(_SHARED_PKG)
            if "build_owned_async_http_client" in _called_names(tree)
        ]
        assert sorted(builders) == [
            "src/hivemind_inference/adapters/anthropic_native.py",
            "src/hivemind_inference/adapters/openai_compatible.py",
        ]

    @pytest.mark.parametrize(
        "relative",
        [
            "src/live_mem/core/consolidator.py",
            "services/graph-memory/src/mcp_memory/core/extractor.py",
            "services/graph-memory/src/mcp_memory/core/embedder.py",
        ],
    )
    def test_every_migrated_consumer_reaches_the_shared_boundary(self, relative):
        source = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "from hivemind_inference" in source
        assert "get_inference_runtime" in source

    def test_the_retired_probe_module_is_gone(self):
        """``core/llm_probe.py`` owned a second, independent provider client.
        Its removal is what makes "one seam" true rather than aspirational."""
        assert not (_CORE_PKG / "core" / "llm_probe.py").exists()


# --------------------------------------------------------------------------- #
# 2. Both services resolve the SAME immutable role profiles                    #
# --------------------------------------------------------------------------- #

class TestSharedProfileResolution:
    def test_core_and_graph_memory_resolve_identical_profiles(self, monkeypatch):
        """The drift this closes: Graph Memory used to carry its own
        ``LLMAAS_*`` defaults (a different chat model, 60000 output tokens,
        temperature 1.0), so the two services could disagree about what "the
        configured model" was while reading one shared ``.env``."""
        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.setenv("LLMAAS_MODEL", "shared-chat-model")
        monkeypatch.setenv("LLMAAS_EMBEDDING_MODEL", "shared-embedding-model")
        monkeypatch.setenv("LLMAAS_EMBEDDING_DIMENSIONS", "768")
        monkeypatch.delenv("PROXY_URL", raising=False)

        from live_mem.config import get_settings as core_settings
        from live_mem.core import inference_runtime as core_runtime
        from mcp_memory.config import get_settings as gm_settings
        from mcp_memory.core import inference_runtime as gm_runtime

        core_settings.cache_clear()
        gm_settings.cache_clear()
        core_runtime.reset_inference_runtime_for_tests()
        gm_runtime.reset_inference_runtime_for_tests()
        try:
            core_config = core_runtime.get_inference_runtime().config
            gm_config = gm_runtime.get_inference_runtime().config
        finally:
            core_runtime.reset_inference_runtime_for_tests()
            gm_runtime.reset_inference_runtime_for_tests()
            core_settings.cache_clear()
            gm_settings.cache_clear()

        assert core_config.chat == gm_config.chat
        assert core_config.embedding == gm_config.embedding
        assert core_config.chat.configured_model == "shared-chat-model"
        assert core_config.embedding.expected_dimensions == 768

    def test_qdrant_vector_width_comes_from_the_resolved_profile(self):
        """The Qdrant collection width must follow the RESOLVED embedding
        profile. Reading the legacy ``LLMAAS_EMBEDDING_DIMENSIONS`` setting
        instead would size collections at its default on every split
        ``INFERENCE_EMBEDDING_*`` deployment — a silently wrong vector space
        that only a full rebuild could repair."""
        from mcp_memory.core.inference_runtime import resolved_vector_dimensions
        from tests.fakes.inference_fakes import gm_inference_runtime

        with gm_inference_runtime(
            embedding=make_embedding_profile(expected_dimensions=768)
        ):
            assert resolved_vector_dimensions() == 768

    def test_qdrant_vector_width_fails_closed_without_an_embedding_role(self):
        from mcp_memory.core.inference_runtime import resolved_vector_dimensions
        from tests.fakes.inference_fakes import gm_inference_runtime

        with gm_inference_runtime(chat=True, embedding=False):
            with pytest.raises(RuntimeError) as excinfo:
                resolved_vector_dimensions()
        assert "INFERENCE_EMBEDDING_" in str(excinfo.value)

    def test_the_vector_store_delegates_to_that_authority(self):
        """Non-vacuity: #277 consumes the whole frozen profile, not a second
        Graph Memory reconstruction of only its vector width."""
        source = (
            _GM_PKG / "core" / "vector_store.py"
        ).read_text(encoding="utf-8")
        assert "resolved_embedding_profile" in source
        assert "settings.llmaas_embedding_dimensions" not in source

    def test_graph_memory_no_longer_declares_its_own_provider_defaults(self):
        """Structural companion: the legacy Graph Memory fields must not carry
        a usable provider default any more, otherwise a split deployment would
        silently keep resolving them."""
        from mcp_memory.config import Settings

        for field in (
            "llmaas_api_url",
            "llmaas_api_key",
            "llmaas_model",
            "llmaas_embedding_model",
        ):
            assert Settings.model_fields[field].default == ""


# --------------------------------------------------------------------------- #
# 3./4. Health: zero cost, unsupported ≠ failure, additive + redacted fields    #
# --------------------------------------------------------------------------- #

def _profiles(endpoint: str):
    return {
        "chat": make_chat_profile(
            endpoint=endpoint, configured_model="emulated-chat-model"
        ),
        "embedding": make_embedding_profile(
            endpoint=endpoint, configured_model="emulated-embedding-model"
        ),
    }


async def _health_block(*, authenticated: bool, emulator_url: str, **kwargs):
    from live_mem.core.inference_runtime import build_llmaas_health_block

    with core_inference_runtime(**_profiles(emulator_url), **kwargs) as runtime:
        try:
            return await build_llmaas_health_block(authenticated=authenticated)
        finally:
            await runtime.aclose()


_PUBLIC_CHILD_KEYS = {
    "status",
    "configured",
    "connectivity",
    "discovery",
    "model_available",
    "readiness",
    "evidence",
}


class TestHealthContract:
    async def test_health_issues_only_discovery_requests(self):
        """Zero chat generation, zero embedding: a health call must never be
        able to spend provider tokens (HM-12 / ADR-0027)."""
        async with InferenceEmulator() as emulator:
            block = await _health_block(
                authenticated=True, emulator_url=emulator.v1_url
            )
        assert block["status"] == "ok"
        assert len(emulator.requests) == 2  # one per configured role
        for request in emulator.requests:
            assert request["method"] == "GET"
            assert request["url"].endswith("/models")
            assert request["body"] == b""

    async def test_unsupported_discovery_is_not_a_provider_failure(self):
        async with InferenceEmulator(
            scripted=[{"status": 405}, {"status": 501}]
        ) as emulator:
            block = await _health_block(
                authenticated=True, emulator_url=emulator.v1_url
            )
        assert block["status"] == "ok"
        for role in ("chat", "embedding"):
            assert block[role]["status"] == "ok"
            assert block[role]["discovery"] == "unsupported"
            assert block[role]["connectivity"] == "reachable"
            assert block[role]["model_available"] is None
            assert block[role]["evidence"] == "connectivity"

    async def test_public_block_is_exactly_the_anonymous_contract(self):
        async with InferenceEmulator() as emulator:
            block = await _health_block(
                authenticated=False, emulator_url=emulator.v1_url
            )
        # Historical top-level shape preserved byte-for-byte on success.
        assert set(block) == {"status", "latency_ms", "chat", "embedding"}
        assert block["status"] == "ok"
        for role in ("chat", "embedding"):
            assert set(block[role]) == _PUBLIC_CHILD_KEYS | {"latency_ms"}
        serialized = json.dumps(block)
        for forbidden in (
            "emulated-chat-model",
            "emulated-embedding-model",
            "openai-compatible",
            "127.0.0.1",
            "test-key",
        ):
            assert forbidden not in serialized

    async def test_authenticated_block_adds_only_safe_identity(self):
        async with InferenceEmulator() as emulator:
            block = await _health_block(
                authenticated=True, emulator_url=emulator.v1_url
            )
        # Historical authenticated fields preserved byte-for-byte.
        assert block["status"] == "ok"
        assert block["model"] == "emulated-chat-model"
        assert block["model_available"] is True
        assert isinstance(block["latency_ms"], float)
        assert set(block["chat"]) == _PUBLIC_CHILD_KEYS | {
            "latency_ms",
            "provider_id",
            "adapter_id",
            "configured_model",
        }
        assert set(block["embedding"]) == _PUBLIC_CHILD_KEYS | {
            "latency_ms",
            "provider_id",
            "adapter_id",
            "configured_model",
            "expected_dimensions",
        }
        serialized = json.dumps(block)
        # Even authenticated, the endpoint and credential never appear.
        assert "127.0.0.1" not in serialized
        assert "test-key" not in serialized

    async def test_error_category_is_authenticated_only(self):
        async with InferenceEmulator(
            scripted=[{"status": 401}, {"status": 401}]
        ) as emulator:
            public = await _health_block(
                authenticated=False, emulator_url=emulator.v1_url
            )
        async with InferenceEmulator(
            scripted=[{"status": 401}, {"status": 401}]
        ) as emulator:
            private = await _health_block(
                authenticated=True, emulator_url=emulator.v1_url
            )
        assert public["status"] == "error"
        assert public["message"] == "LLMaaS unreachable"
        assert "error_category" not in public["chat"]
        assert private["chat"]["error_category"] == "auth"

    async def test_unconfigured_roles_report_not_configured(self):
        from live_mem.core.inference_runtime import build_llmaas_health_block

        with core_inference_runtime(chat=False, embedding=False):
            block = await build_llmaas_health_block(authenticated=True)
        assert block["status"] == "warning"
        assert block["message"] == "LLMaaS is not configured"
        for role in ("chat", "embedding"):
            assert block[role]["configured"] is False
            assert block[role]["connectivity"] == "not_configured"
            assert block[role]["discovery"] == "not_run"
            assert block[role]["readiness"] == "unknown"
            assert block[role]["evidence"] == "none"

    async def test_embedding_only_deployment_keeps_the_chat_warning(self):
        """An embedding-only deployment is legal: the historical top-level
        block follows the CHAT role (it always did), while the embedding child
        reports its own real state."""
        from live_mem.core.inference_runtime import build_llmaas_health_block

        async with InferenceEmulator() as emulator:
            with core_inference_runtime(
                chat=False,
                embedding=make_embedding_profile(
                    endpoint=emulator.v1_url,
                    configured_model="emulated-embedding-model",
                ),
            ) as runtime:
                try:
                    block = await build_llmaas_health_block(authenticated=True)
                finally:
                    await runtime.aclose()
        assert block["status"] == "warning"
        assert block["chat"]["configured"] is False
        assert block["embedding"]["configured"] is True
        assert block["embedding"]["status"] == "ok"

    @pytest.mark.parametrize(
        "failure,expected_category",
        [
            (lambda: InferenceConfigError(["mixed families"]), "invalid_request"),
            (
                lambda: InferenceRuntimeClosed("service is shutting down"),
                "unavailable",
            ),
        ],
        ids=["invalid-configuration", "shutting-down"],
    )
    async def test_unavailable_runtime_keeps_the_historical_error_envelope(
        self, monkeypatch, failure, expected_category
    ):
        """PR #303 round 1 (Codex Sol, low): health is total by contract AND
        byte-compatible.

        An environment that startup would refuse — or a service already shutting
        down — must still produce the DOCUMENTED top-level error envelope. A new
        top-level ``message`` would break a consumer parsing the historical
        shape, which this lot's acceptance criteria forbid. The specific cause
        belongs in the additive role fields, and only on the authenticated
        surface.
        """
        from live_mem.core import inference_runtime as core_runtime

        def _boom():
            raise failure()

        monkeypatch.setattr(core_runtime, "get_inference_runtime", _boom)

        public = await core_runtime.build_llmaas_health_block(authenticated=False)
        assert public["status"] == "error"
        assert public["message"] == "LLMaaS unreachable"
        for role in ("chat", "embedding"):
            assert set(public[role]) == _PUBLIC_CHILD_KEYS
            assert public[role]["status"] == "error"
            assert public[role]["configured"] is False
            assert public[role]["connectivity"] == "not_configured"

        private = await core_runtime.build_llmaas_health_block(authenticated=True)
        assert private["status"] == "error"
        assert private["message"] == "LLMaaS unreachable"
        for role in ("chat", "embedding"):
            assert private[role]["error_category"] == expected_category

    async def test_one_broken_role_does_not_blank_the_other(self, monkeypatch):
        """A probe whose CONSTRUCTION raises (rather than returning an error
        result) must be contained: the sibling role still reports its real
        state instead of the whole block degrading."""
        from hivemind_inference import registry
        from live_mem.core import inference_runtime as core_runtime

        def _boom(profile, *, proxy_url=None):
            raise RuntimeError("transport construction exploded")

        monkeypatch.setattr(registry, "build_embedding_probe", _boom)

        async with InferenceEmulator() as emulator:
            with core_inference_runtime(**_profiles(emulator.v1_url)) as runtime:
                try:
                    block = await core_runtime.build_llmaas_health_block(
                        authenticated=True
                    )
                finally:
                    await runtime.aclose()
        assert block["status"] == "ok"
        assert block["chat"]["status"] == "ok"
        assert block["embedding"]["status"] == "error"
        assert block["embedding"]["connectivity"] == "unreachable"
        assert block["embedding"]["error_category"] == "unavailable"


# --------------------------------------------------------------------------- #
# Startup validation and shutdown wiring                                       #
# --------------------------------------------------------------------------- #

class TestStartupAndShutdownWiring:
    @pytest.fixture(autouse=True)
    def _no_ambient_env_file(self, monkeypatch, tmp_path):
        """Startup resolution reads ``.env`` RELATIVE TO THE WORKING DIRECTORY.

        ``merged_environment`` overlays ``os.environ`` on top of the file, so a
        key present only in a developer's own ``.env`` survives
        ``monkeypatch.delenv`` and quietly changes what these tests resolve.
        The file is gitignored and the deployment guide tells developers to
        create one, so this is a real local-only failure — CI never sees it.
        """
        monkeypatch.chdir(tmp_path)
        assert not list(tmp_path.glob(".env"))

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    def test_startup_validation_is_fail_closed_on_a_mixed_family(
        self, monkeypatch, module_path
    ):
        import importlib

        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.setenv("INFERENCE_CHAT_PROVIDER", "openai-compatible")
        monkeypatch.delenv("PROXY_URL", raising=False)

        module = importlib.import_module(module_path)
        module.reset_inference_runtime_for_tests()
        try:
            with pytest.raises(InferenceConfigError):
                module.validate_inference_startup()
        finally:
            module.reset_inference_runtime_for_tests()

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    def test_startup_validation_accepts_a_wholly_absent_provider(
        self, monkeypatch, module_path
    ):
        import importlib

        apply_graph_memory_baseline_env(monkeypatch)
        for name in (
            "LLMAAS_API_URL",
            "LLMAAS_API_KEY",
            "LLMAAS_MODEL",
            "LLMAAS_EMBEDDING_MODEL",
            "LLMAAS_EMBEDDING_DIMENSIONS",
            "INFERENCE_CHAT_PROVIDER",
            "PROXY_URL",
        ):
            monkeypatch.delenv(name, raising=False)

        module = importlib.import_module(module_path)
        module.reset_inference_runtime_for_tests()
        try:
            module.validate_inference_startup()
            assert module.get_inference_runtime().config.configured_roles == ()
        finally:
            module.reset_inference_runtime_for_tests()

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    async def test_shutdown_cannot_be_undone_by_a_late_background_worker(
        self, monkeypatch, module_path
    ):
        """PR #303 round 1 (Codex Sol, high): the resurrection leak.

        Both services run untracked ``asyncio`` workers (consolidation queue,
        ingestion queue) that reach inference, and neither lifespan awaits them
        before closing the runtime. The close hook used to drop the singleton
        and return, so the very next ``get_inference_runtime()`` from a worker
        still in flight silently built a REPLACEMENT runtime — whose transport
        nothing would ever close, because the only close hook had already run.
        """
        import importlib

        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.delenv("PROXY_URL", raising=False)

        module = importlib.import_module(module_path)
        module.reset_inference_runtime_for_tests()
        try:
            first = module.get_inference_runtime()
            transport = first.chat_provider()._owned_http_client
            await module.close_inference_runtime_if_initialized()
            assert transport.is_closed

            # A worker reaching inference after shutdown fails CLOSED instead
            # of opening an unowned transport.
            with pytest.raises(InferenceRuntimeClosed):
                module.get_inference_runtime()

            # A new SERVING WINDOW (an explicit startup) legitimately reopens
            # the seam: that window's own shutdown will own the new runtime.
            module.validate_inference_startup()
            second = module.get_inference_runtime()
            assert second is not first
            await module.close_inference_runtime_if_initialized()
        finally:
            module.reset_inference_runtime_for_tests()

    # -- the close-failed / cancelled / wedged rows of the lifecycle matrix -- #
    #
    # PR #303 rounds 1-3 produced FOUR findings of one class: who owns the
    # runtime slot, and when may it be cleared or adopted. The systemic audit
    # that followed showed why each point fix held: no test ever drove a close
    # that did not return normally, so a mutant swallowing the failure and
    # clearing the slot anyway survived the entire suite. These rows are that
    # missing column, parametrised over both services because the two holders
    # were byte-identical and every earlier repair landed in only one of them.

    @staticmethod
    def _with_lifecycle_adapter(module, mode="ok"):
        """A resolved runtime whose single populated slot is a controllable
        close, installed as the service's runtime."""
        from tests.fakes.inference_fakes import LifecycleAdapter

        module.reset_inference_runtime_for_tests()
        runtime = module.get_inference_runtime()
        adapter = LifecycleAdapter(mode=mode)
        runtime._chat_provider = adapter
        return runtime, adapter

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    async def test_a_failed_close_leaves_the_service_permanently_unconfirmed(
        self, monkeypatch, module_path
    ):
        """A close that RAISES must leave the runtime where a reference to it
        survives, must never be mistaken for a completed shutdown, and must
        never be silently "recovered" by a later call.

        This is the hole the audit's mutant H5 walked through: swallow the
        failure and clear the slot anyway, and 4307 tests stayed green while a
        possibly-open transport became unreachable. The sweep then showed the
        opposite fix — retain and retry — was equally hollow, because a retried
        httpx close is a no-op that reports success (S2-F1). The honest end
        state is that this service can no longer start a serving window.
        """
        import importlib

        from hivemind_inference.runtime import InferenceShutdownIncomplete

        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.delenv("PROXY_URL", raising=False)
        module = importlib.import_module(module_path)

        runtime, adapter = self._with_lifecycle_adapter(module, mode="raise")
        try:
            adapter.release.set()
            with pytest.raises(RuntimeError, match="transport close failed"):
                await module.close_inference_runtime_if_initialized()

            # Retained: the holder still points at it, the slot still holds the
            # adapter, and the runtime does not claim to be closed.
            assert module._holder.current is runtime
            assert runtime._chat_provider is adapter
            assert not runtime.is_fully_closed
            assert adapter.calls == 1

            # A late worker is refused meanwhile — the window is shut down.
            with pytest.raises(InferenceRuntimeClosed):
                module.get_inference_runtime()

            # A NEW serving window refuses to start over it rather than
            # adopting a runtime whose transports are unaccounted for.
            with pytest.raises(InferenceRuntimeClosed, match="has not finished"):
                module.validate_inference_startup()

            # And that refusal is PERMANENT for this process: a further close
            # neither re-invokes the adapter nor reports success, even though
            # the adapter would now succeed.
            adapter.mode = "ok"
            with pytest.raises(InferenceShutdownIncomplete):
                await module.close_inference_runtime_if_initialized()
            assert adapter.calls == 1, "a no-op retry was issued anyway"
            assert module._holder.current is runtime
            assert not runtime.is_fully_closed
            with pytest.raises(InferenceRuntimeClosed, match="has not finished"):
                module.validate_inference_startup()
        finally:
            module.reset_inference_runtime_for_tests()

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    async def test_a_cancelled_but_settled_close_still_clears_the_holder(
        self, monkeypatch, module_path
    ):
        """Cancellation is bookkeeping, not evidence.

        The drain finishes every started close before re-raising, so when the
        cancellation surfaces the transports really are released. Clearing the
        slot from ``aclose()`` RETURNING NORMALLY therefore lost a perfectly
        complete shutdown and pinned the holder; clearing it from the runtime's
        settled state does not.
        """
        import asyncio
        import importlib

        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.delenv("PROXY_URL", raising=False)
        module = importlib.import_module(module_path)

        runtime, adapter = self._with_lifecycle_adapter(module)
        try:
            closing = asyncio.create_task(
                module.close_inference_runtime_if_initialized()
            )
            await adapter.entered.wait()
            closing.cancel()
            adapter.release.set()
            with pytest.raises(asyncio.CancelledError):
                await closing

            assert runtime.is_fully_closed
            assert module._holder.current is None
            # The next window starts cleanly on a fresh runtime.
            module.validate_inference_startup()
            assert module._holder.current is not runtime
        finally:
            module.reset_inference_runtime_for_tests()

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    async def test_a_close_that_never_returns_ends_within_the_shutdown_budget(
        self, monkeypatch, module_path
    ):
        """A wedged provider transport must not become a container only SIGKILL
        can end.

        Neither service's shutdown hook bounds the close, and the drain used to
        absorb its caller's own cancellation and then block again — so even
        ``wait_for`` could not bound it. The budget makes shutdown TERMINATE and
        names the slot instead of abandoning it.
        """
        import asyncio
        import importlib

        import hivemind_inference.runtime as runtime_module
        from hivemind_inference.runtime import InferenceShutdownIncomplete

        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.delenv("PROXY_URL", raising=False)
        monkeypatch.setattr(runtime_module, "_CLOSE_BUDGET_SECONDS", 0.05)
        module = importlib.import_module(module_path)

        runtime, adapter = self._with_lifecycle_adapter(module, mode="hang")
        closing = asyncio.create_task(
            module.close_inference_runtime_if_initialized()
        )
        try:
            # The safety net is a NON-cancelling wait on purpose. `wait_for`
            # would be useless here: it bounds by cancelling, and the drain
            # deliberately absorbs cancellations — so without the budget this
            # test would HANG rather than fail, which is the worse outcome in
            # CI. Observing the task instead turns the same defect into a
            # clean assertion failure.
            done, _pending = await asyncio.wait({closing}, timeout=2.0)
            assert done, "shutdown did not terminate within its own budget"

            with pytest.raises(InferenceShutdownIncomplete) as caught:
                await closing
            assert caught.value.slots == ("_chat_provider",)
            # Named, not abandoned: both the adapter and its in-flight close
            # survive, so the transport still has an owner.
            assert runtime._chat_provider is adapter
            assert "_chat_provider" in runtime._close_tasks
            assert module._holder.current is runtime
        finally:
            closing.cancel()
            for task in list(runtime._close_tasks.values()):
                task.cancel()
            module.reset_inference_runtime_for_tests()

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    async def test_a_finishing_close_never_clears_a_slot_it_no_longer_owns(
        self, monkeypatch, module_path
    ):
        """The close holds its runtime across an ``await``; the slot may have
        moved on. Clearing unconditionally would drop a live replacement."""
        import asyncio
        import importlib

        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.delenv("PROXY_URL", raising=False)
        module = importlib.import_module(module_path)

        _runtime, adapter = self._with_lifecycle_adapter(module)
        try:
            closing = asyncio.create_task(
                module.close_inference_runtime_if_initialized()
            )
            await adapter.entered.wait()
            module.reset_inference_runtime_for_tests()
            replacement = module.get_inference_runtime()
            adapter.release.set()
            await closing
            assert module._holder.current is replacement
        finally:
            module.reset_inference_runtime_for_tests()

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    def test_a_failed_startup_does_not_reopen_the_seam(
        self, monkeypatch, module_path
    ):
        """A startup that raises must leave the terminal flag as it found it.

        The lifespan aborts on that raise, so this process will never run a
        close hook — and a worker surviving the previous window would build a
        provider transport nobody is left to release.
        """
        import importlib

        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.delenv("PROXY_URL", raising=False)
        module = importlib.import_module(module_path)
        module.reset_inference_runtime_for_tests()
        try:
            module.validate_inference_startup()
            asyncio_run = __import__("asyncio").run
            asyncio_run(module.close_inference_runtime_if_initialized())
            assert module._holder.is_shut_down

            monkeypatch.setenv("INFERENCE_CHAT_PROVIDER", "openai-compatible")
            with pytest.raises(InferenceConfigError):
                module.validate_inference_startup()

            assert module._holder.is_shut_down
            with pytest.raises(InferenceRuntimeClosed):
                module.get_inference_runtime()
        finally:
            module.reset_inference_runtime_for_tests()

    @pytest.mark.parametrize(
        ("module_path", "service"),
        [
            ("live_mem.core.inference_runtime", "Hivemind"),
            ("mcp_memory.core.inference_runtime", "Graph Memory"),
        ],
    )
    def test_each_service_keeps_its_own_operator_facing_refusals(
        self, monkeypatch, module_path, service
    ):
        """The two services now SHARE one state machine, so the only thing that
        still distinguishes their operator messages is configuration.

        Nothing asserted those strings before, so relabelling Graph Memory's
        refusals as the core's survived the whole suite — and an operator
        reading "Hivemind inference runtime" in the Graph Memory log would look
        for the fault in the wrong service.
        """
        import importlib

        apply_graph_memory_baseline_env(monkeypatch)
        monkeypatch.setenv("LLMAAS_API_URL", _EXTERNAL_ORIGIN)
        monkeypatch.setenv("LLMAAS_API_KEY", "test-key")
        monkeypatch.delenv("PROXY_URL", raising=False)
        module = importlib.import_module(module_path)
        module.reset_inference_runtime_for_tests()
        try:
            runtime, adapter = self._with_lifecycle_adapter(module, mode="raise")
            adapter.release.set()
            asyncio_run = __import__("asyncio").run
            with pytest.raises(RuntimeError):
                asyncio_run(module.close_inference_runtime_if_initialized())

            with pytest.raises(InferenceRuntimeClosed) as shut_down:
                module.get_inference_runtime()
            assert str(shut_down.value) == (
                f"the {service} inference runtime has been shut down; no new "
                "provider transport may be built"
            )

            with pytest.raises(InferenceRuntimeClosed) as refused:
                module.validate_inference_startup()
            assert str(refused.value) == (
                f"the previous {service} serving window has not finished "
                "releasing its provider transports; refusing to start a new "
                "one over them"
            )
            assert runtime._chat_provider is adapter
        finally:
            module.reset_inference_runtime_for_tests()

    @pytest.mark.parametrize(
        "module_path",
        ["live_mem.core.inference_runtime", "mcp_memory.core.inference_runtime"],
    )
    def test_both_holders_resolve_from_the_same_env_file(
        self, monkeypatch, module_path
    ):
        """``env_file=".env"`` moved from two inline literals into one holder
        default, where nothing distinguishes it from ``None`` — which would
        silently stop reading the operator's file."""
        import importlib

        from hivemind_inference import holder as holder_module
        from tests.fakes.inference_fakes import make_runtime

        apply_graph_memory_baseline_env(monkeypatch)
        module = importlib.import_module(module_path)
        module.reset_inference_runtime_for_tests()
        seen = {}

        def _record(environ=None, *, env_file=None, proxy_url=None):
            seen["env_file"] = env_file
            # Built directly rather than delegating to the real resolver: the
            # patched attribute IS the resolver, so calling through would
            # recurse.
            return make_runtime(proxy_url=proxy_url, chat=False, embedding=False)

        monkeypatch.setattr(holder_module.InferenceRuntime, "from_environment", _record)
        try:
            module.get_inference_runtime()
            assert seen["env_file"] == ".env"
        finally:
            module.reset_inference_runtime_for_tests()

    def test_the_runtime_fixture_restores_the_whole_holder_state(self):
        """The fixture must restore every lifecycle field, not the ones whoever
        wrote it happened to remember.

        A leaked terminal flag poisons unrelated tests in whatever order they
        run — which is how the previous field-by-field fixture behaved when the
        holder grew a second field.
        """
        from live_mem.core import inference_runtime as core_runtime

        core_runtime._holder.restore_for_tests((None, True))  # a shut-down window
        try:
            with core_inference_runtime(chat=True):
                assert not core_runtime._holder.is_shut_down
            assert core_runtime._holder.is_shut_down, (
                "the fixture restored the runtime but leaked the terminal flag"
            )
        finally:
            core_runtime.reset_inference_runtime_for_tests()

    def test_the_core_inference_lifecycle_is_not_bound_to_the_mcp_session(self):
        """PR #303 round 3 sweep (L1-F1): the runtime lifecycle must NOT live in
        ``_lifespan``.

        That hook is the low-level MCP server's, and
        ``StreamableHTTPSessionManager`` calls ``Server.run()`` — which enters
        it — once PER SESSION. A process-wide singleton owning provider
        transports attached there means one client disconnecting closes them
        for everyone: ``/health`` then reports an outage without so much as
        probing the provider, and a concurrent session is refused. The
        lifecycle belongs to the process's own ASGI lifespan.
        """
        source = (_CORE_PKG / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        lifespan = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_lifespan"
        )
        called = _called_names(lifespan)
        assert "validate_inference_startup" not in called
        assert "close_inference_runtime_if_initialized" not in called

    def test_the_core_process_lifespan_validates_and_closes_outermost(self):
        """The replacement must actually carry the lifecycle, and be the
        OUTERMOST layer so a real uvicorn shutdown always reaches it.

        Since #306 the replacement is the SHARED ``LifespanGuard``, not a
        Core-local wrapper. The structural invariant is unchanged and is
        asserted the same way: the inference startup check and the inference
        transport release must both be registered as hooks, and no other
        middleware may wrap the guard.
        """
        source = (_CORE_PKG / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        create_app = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "create_app"
        )
        wraps = [
            node
            for node in ast.walk(create_app)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "LifespanGuard"
        ]
        assert wraps, "create_app() does not install LifespanGuard"
        last_wrap = max(node.lineno for node in wraps)
        other_wraps = [
            node.lineno
            for node in ast.walk(create_app)
            if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id.endswith("Middleware")
        ]
        assert all(line < last_wrap for line in other_wraps), (
            "LifespanGuard is not the outermost layer"
        )

        registered = _registered_hooks(create_app)
        assert registered.get("on_validate", []) == [], (
            "process ownership polluted the pure validation phase"
        )
        assert registered["ownership"] == [
            "LifespanOwnership(reserve=window.claim, "
            "release_reusable=window.release)"
        ], "the process window is not bound to the positive lifecycle contract"
        assert registered["on_startup"] == [
            "window.guard(_migrate_target_pairing_admission_anchors)",
            "window.guard(_validate_inference_startup)"
        ], "the process startup hooks are not owner-guarded"
        # SIBLINGS, not nested: the guard runs every on_shutdown entry through
        # `run_finalizers`, so a consolidator close that raises cannot skip the
        # transport release. Folding one into the other would forfeit that.
        # Process-window release is not in this list: it needs a positive
        # lifecycle verdict after every sibling has settled.
        assert registered["on_shutdown"] == [
            "window.guard(_close_core_process_resources)",
            "window.guard(_close_inference_runtime)",
        ]

        for name in ("_validate_inference_startup", "_close_inference_runtime"):
            hook = next(
                node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == name
            )
            called = _called_names(hook)
            assert called & {
                "validate_inference_startup",
                "close_inference_runtime_if_initialized",
            }, f"{name} reaches no inference lifecycle call"

    def test_graph_memory_factory_validates_and_shutdown_closes(self):
        """Graph Memory registers the same two hooks on the same shared guard.

        The check moved out of ``main()``: ``main()`` is only one entrypoint,
        and validating there resolved a runtime outside any window able to
        release it. ``_create_app()`` is where the guard — and therefore the
        process gate — is built.
        """
        source = (_GM_PKG / "server.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        factory = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_create_app"
        )
        registered = _registered_hooks(factory)
        assert registered.get("on_validate", []) == []
        assert registered["ownership"] == [
            "LifespanOwnership(reserve=window.claim, "
            "release_reusable=window.release)"
        ]
        assert registered["on_startup"] == [
            "window.guard(_validate_inference_startup)",
            "window.guard(_initialize_graph_document_schema)",
        ]
        assert registered["on_shutdown"] == [
            "window.guard(_close_llm_singletons)",
            "window.guard(_close_inference_runtime)",
        ], "the extractor/embedder close and the transport release are not siblings"

        main_fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        assert "validate_inference_startup" not in _called_names(main_fn), (
            "main() still validates outside the guard's startup gate"
        )
        close_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_close_inference_runtime"
        )
        assert "close_inference_runtime_if_initialized" in _called_names(close_fn)
        schema_fn = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_initialize_graph_document_schema"
        )
        assert {
            "get_graph",
            "ensure_document_lookup_index",
            "initialize_document_schema",
        } <= _called_names(schema_fn)


# --------------------------------------------------------------------------- #
# Inference lifecycle through the shared guard                                 #
# --------------------------------------------------------------------------- #
#
# What used to live here was a per-service ASGI wrapper and its finalisation
# rules. #306 replaced both wrappers with one shared `LifespanGuard`, so the
# GENERIC properties — one terminal verdict per phase, a duplicate never
# improving it, silence counting as failure, a cancellation announced before it
# is re-raised, every finaliser attempted — are asserted against that guard by
# `tests/test_asgi_lifespan_guard.py` and `tests/test_asgi_lifespan_integration.py`.
# Re-asserting them here would pin a second, weaker copy of a contract that now
# has one owner.
#
# What remains here is only what is specific to the INFERENCE consumers: that
# an invalid inference configuration really stops the server, that a valid one
# does not break an ordinary startup, that a deployment with no lifespan is
# refused instead of silently unvalidated, and that the transport release
# cannot be skipped by the sibling closer failing.
#
# PR #303 round 4 established the method these tests keep: drive
# `uvicorn.lifespan.on.LifespanOn`, never the wrapper. A test that drives the
# wrapper only observes the messages its author chose to look for, and uvicorn
# — the component whose interpretation decides whether the process stops —
# disagreed twice.


class _RecordingInnerApp:
    """ASGI stand-in for the wrapped application.

    It records the lifespan messages it RECEIVES and completes its own cycle,
    which is the property R3-F1 destroyed: an inner app that is never told to
    shut down never tears its own resources down either.
    """

    def __init__(self) -> None:
        self.received: list[str] = []

    async def __call__(self, scope, receive, send):
        while True:
            message = await receive()
            self.received.append(message["type"])
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def _core_app(monkeypatch, inner):
    """Build the real Core ASGI stack around ``inner``.

    The bootstrap-key guard and Mesh are neutralised: they run before the
    subject of these tests and have their own coverage.
    """
    from live_mem import server as core_server

    monkeypatch.setattr(core_server, "_reject_weak_bootstrap_key", lambda _key: None)
    monkeypatch.setattr(core_server.settings, "hivemind_mesh_enabled", "false")
    monkeypatch.setattr(core_server.mcp, "streamable_http_app", lambda: inner)
    return core_server.create_app()


class TestInferenceLifecycleThroughTheGuard:
    """PR #303 round 3-6 findings, re-anchored on the shared guard (#306)."""

    @staticmethod
    async def _uvicorn_startup(app):
        import uvicorn
        from uvicorn.lifespan.on import LifespanOn

        # ``log_config=None`` matters: ``uvicorn.Config.__init__`` calls
        # ``configure_logging()``, which applies a ``dictConfig`` to the
        # PROCESS. Leaving it on lets these tests reconfigure logging for every
        # test that runs after them — a suite-wide side effect from a test
        # about shutdown verdicts.
        state = LifespanOn(uvicorn.Config(app, lifespan="auto", log_config=None))
        await state.startup()
        return state

    async def test_an_invalid_configuration_actually_stops_uvicorn(
        self, monkeypatch
    ):
        """The acceptance criterion is that an invalid configuration blocks
        serving. Raising out of the lifespan does NOT achieve that: under
        ``lifespan="auto"`` uvicorn reads an exception as "this app has no
        lifespan", logs ``Application startup complete`` and serves on.
        """
        from live_mem.core import inference_runtime as core_runtime

        def _refuse():
            raise InferenceConfigError("mixed LLMAAS_*/INFERENCE_* family")

        monkeypatch.setattr(core_runtime, "validate_inference_startup", _refuse)
        inner = _RecordingInnerApp()
        state = await self._uvicorn_startup(_core_app(monkeypatch, inner))
        assert state.startup_failed, "uvicorn did not treat this as a failed startup"
        assert state.should_exit, "the process would have served on"
        assert inner.received == [], (
            "the inner application was started over a refused configuration"
        )

    async def test_a_valid_configuration_starts_normally_through_uvicorn(
        self, monkeypatch
    ):
        """Non-vacuity for the test above: the startup hook must not break an
        ordinary startup, and the ordinary shutdown must stay clean."""
        import asyncio

        from live_mem.core import inference_runtime as core_runtime

        validated = []
        monkeypatch.setattr(
            core_runtime,
            "validate_inference_startup",
            lambda: validated.append(True),
        )
        inner = _RecordingInnerApp()
        state = await self._uvicorn_startup(_core_app(monkeypatch, inner))
        assert not state.startup_failed
        assert not state.should_exit
        assert validated, "the startup hook never ran"
        assert inner.received == ["lifespan.startup"]

        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert not state.shutdown_failed
        assert not state.error_occurred
        assert inner.received == ["lifespan.startup", "lifespan.shutdown"]

    async def test_a_lifespan_less_deployment_is_refused_not_unvalidated(
        self, monkeypatch
    ):
        """R5-F1, answered by the guard instead of by a second gate.

        ``--lifespan off`` is a standard uvicorn option and dispatches no
        lifespan scope. #303 answered it by validating a second time inside
        ``create_app()``, which resolves and publishes a runtime in a process
        that may never open a window able to release it. Declaring lifecycle
        hooks answers it without acquiring anything: the guard refuses every
        request, including health and metrics, and the validation never runs.
        """
        from hivemind_inference.asgi_lifespan import StartupRefused
        from live_mem.core import inference_runtime as core_runtime

        called = []
        served = []

        monkeypatch.setattr(
            core_runtime,
            "validate_inference_startup",
            lambda: called.append(True),
        )

        async def inner(scope, receive, send):
            served.append(scope["type"])

        app = _core_app(monkeypatch, inner)
        with pytest.raises(StartupRefused):
            await app({"type": "http", "path": "/health"}, None, None)
        assert served == [], "a request was served without a lifespan"
        assert called == [], (
            "the configuration was resolved outside any releasable window"
        )

    async def test_the_transport_release_survives_a_failing_sibling_closer(
        self, monkeypatch
    ):
        """The round-3 class sweep, expressed against the sibling-hook shape.

        Cleanup used to be a flat sequence of awaits with the step owning the
        provider transports written LAST, so any earlier failure skipped it and
        leaked the transport. The two closers are now independent
        ``on_shutdown`` entries, and the guard runs every entry through
        ``run_finalizers`` — so the release still happens, and the verdict is
        still ``failed``.
        """
        import asyncio

        apply_graph_memory_baseline_env(monkeypatch)
        from mcp_memory import server as gm
        from hivemind_inference.process_window import ProcessWindowGate

        released = []
        monkeypatch.setattr(
            gm, "_process_window", ProcessWindowGate(service="Graph Memory test")
        )

        async def _boom():
            raise RuntimeError("extractor close failed")

        async def _release():
            released.append("transport")

        monkeypatch.setattr(gm, "_close_llm_singletons", _boom)
        monkeypatch.setattr(gm, "_close_inference_runtime", _release)
        monkeypatch.setattr(gm, "_validate_inference_startup", lambda: None)
        monkeypatch.setattr(gm, "_initialize_graph_document_schema", lambda: None)
        monkeypatch.setattr(gm.mcp, "streamable_http_app", lambda: _RecordingInnerApp())

        state = await TestInferenceLifecycleThroughTheGuard._uvicorn_startup(
            gm._create_app()
        )
        assert not state.startup_failed
        await asyncio.wait_for(state.shutdown(), timeout=2.0)

        assert released == ["transport"], (
            "a failing sibling closer skipped the transport release"
        )
        assert state.shutdown_failed, "the failed cleanup was reported as clean"

    async def test_graph_schema_migration_finishes_before_inner_startup(
        self, monkeypatch
    ):
        apply_graph_memory_baseline_env(monkeypatch)
        from mcp_memory import server as gm
        from hivemind_inference.process_window import ProcessWindowGate

        schema_entered = asyncio.Event()
        release_schema = asyncio.Event()
        inner = _RecordingInnerApp()
        monkeypatch.setattr(
            gm, "_process_window", ProcessWindowGate(service="Graph Memory test")
        )

        async def initialize_schema():
            schema_entered.set()
            await release_schema.wait()

        monkeypatch.setattr(gm, "_validate_inference_startup", lambda: None)
        monkeypatch.setattr(
            gm, "_initialize_graph_document_schema", initialize_schema
        )
        monkeypatch.setattr(gm, "_close_llm_singletons", lambda: None)
        monkeypatch.setattr(gm, "_close_inference_runtime", lambda: None)
        monkeypatch.setattr(gm.mcp, "streamable_http_app", lambda: inner)

        startup = asyncio.create_task(self._uvicorn_startup(gm._create_app()))
        await schema_entered.wait()
        await asyncio.sleep(0)
        assert startup.done() is False
        assert inner.received == []

        release_schema.set()
        state = await asyncio.wait_for(startup, timeout=2.0)
        assert not state.startup_failed
        assert inner.received == ["lifespan.startup"]
        await asyncio.wait_for(state.shutdown(), timeout=2.0)
        assert not state.shutdown_failed

    async def test_graph_schema_failure_refuses_startup_before_inner_app(
        self, monkeypatch
    ):
        apply_graph_memory_baseline_env(monkeypatch)
        from mcp_memory import server as gm
        from hivemind_inference.process_window import ProcessWindowGate

        inner = _RecordingInnerApp()
        monkeypatch.setattr(
            gm, "_process_window", ProcessWindowGate(service="Graph Memory test")
        )

        async def fail_schema():
            raise RuntimeError("document schema initialization is unavailable")

        monkeypatch.setattr(gm, "_validate_inference_startup", lambda: None)
        monkeypatch.setattr(gm, "_initialize_graph_document_schema", fail_schema)
        monkeypatch.setattr(gm, "_close_llm_singletons", lambda: None)
        monkeypatch.setattr(gm, "_close_inference_runtime", lambda: None)
        monkeypatch.setattr(gm.mcp, "streamable_http_app", lambda: inner)

        state = await self._uvicorn_startup(gm._create_app())

        assert state.startup_failed
        assert inner.received == []

    async def test_graph_schema_hook_has_a_fixed_fail_closed_deadline(
        self, monkeypatch
    ):
        apply_graph_memory_baseline_env(monkeypatch)
        from mcp_memory import server as gm

        entered = asyncio.Event()
        never = asyncio.Event()

        class _HangingGraph:
            async def ensure_document_lookup_index(self):
                return None

            async def initialize_document_schema(self):
                entered.set()
                await never.wait()

        monkeypatch.setattr(gm, "get_graph", lambda: _HangingGraph())
        monkeypatch.setattr(
            gm,
            "_GRAPH_SCHEMA_STARTUP_TIMEOUT_SECONDS",
            0.01,
            raising=False,
        )

        with pytest.raises(RuntimeError, match="document schema initialization"):
            await asyncio.wait_for(
                gm._initialize_graph_document_schema(),
                timeout=0.1,
            )
        assert entered.is_set()


class TestProcessWindowContract:
    """Unit-level contract for `ProcessWindow`.

    Atomic owner identity, owner-guarded resource hooks, and the guard's paired
    reserve/positive-release phase are all load-bearing. This unit suite pins
    the compare/store boundary and stale-owner behavior; the structural and
    end-to-end suites below pin the lifecycle placement.
    """

    @staticmethod
    def _gate():
        from hivemind_inference.process_window import ProcessWindowGate

        return ProcessWindowGate(service="Test")

    def test_a_second_window_is_refused_and_the_first_keeps_ownership(self):
        from hivemind_inference.process_window import ProcessWindowBusy

        gate = self._gate()
        first, second = gate.new_window(), gate.new_window()
        first.claim()
        with pytest.raises(ProcessWindowBusy, match="already owns"):
            second.claim()
        assert first.owns() and not second.owns()

    def test_the_same_window_cannot_claim_twice_while_it_is_active(self):
        from hivemind_inference.process_window import ProcessWindowBusy

        gate = self._gate()
        window = gate.new_window()
        window.claim()
        with pytest.raises(ProcessWindowBusy, match="already owns"):
            window.claim()
        assert window.owns()

    def test_release_is_owner_scoped(self):
        gate = self._gate()
        first, second = gate.new_window(), gate.new_window()
        first.claim()
        second.release()  # not the owner: must not steal the window
        assert first.owns()
        first.release()
        assert gate.owner is None
        second.claim()  # now free
        assert second.owns()
        first.release()  # a delayed duplicate cannot clear the later owner
        assert second.owns()

    def test_claim_is_atomic_across_threads(self):
        """Both contenders reach the ownership boundary together.

        The injected lock preserves the production critical section while
        making the scheduling deterministic: the first two acquisitions wait
        at one barrier, then the real lock admits exactly one compare/store at
        a time.
        """
        import queue
        import threading

        gate = self._gate()
        contenders = [gate.new_window(), gate.new_window()]
        rendezvous = threading.Barrier(2)
        real_lock = threading.Lock()
        count_lock = threading.Lock()
        entered = 0

        class RendezvousLock:
            def __enter__(self):
                nonlocal entered
                with count_lock:
                    entered += 1
                    wait_for_peer = entered <= 2
                if wait_for_peer:
                    rendezvous.wait(timeout=2.0)
                real_lock.acquire()
                return self

            def __exit__(self, exc_type, exc, traceback):
                real_lock.release()

        gate._lock = RendezvousLock()
        outcomes = queue.Queue()

        def claim(window):
            try:
                window.claim()
            except BaseException as exc:  # noqa: BLE001 - recorded by thread
                outcomes.put(exc)
            else:
                outcomes.put(window)

        threads = [
            threading.Thread(target=claim, args=(window,))
            for window in contenders
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2.0)
            assert not thread.is_alive()

        observed = [outcomes.get_nowait(), outcomes.get_nowait()]
        accepted = [item for item in observed if item in contenders]
        refused = [
            item
            for item in observed
            if isinstance(item, BaseException)
        ]
        assert len(accepted) == 1
        assert len(refused) == 1
        assert type(refused[0]).__name__ == "ProcessWindowBusy"
        assert gate.owner is accepted[0]
        assert entered >= 2, "claim bypassed the serialized owner boundary"

    def test_claim_reads_and_writes_the_owner_inside_one_lock_scope(self):
        """Structural companion to the scheduled thread proof.

        The rendezvous test above kills removal of the lock. This assertion
        also kills the subtler mutant that reads under the lock but moves the
        store outside it.
        """
        import ast
        import inspect
        import textwrap

        from hivemind_inference.process_window import ProcessWindow

        tree = ast.parse(textwrap.dedent(inspect.getsource(ProcessWindow.claim)))
        critical_sections = [
            node for node in ast.walk(tree) if isinstance(node, ast.With)
        ]
        assert len(critical_sections) == 1
        inside = {
            id(node)
            for node in ast.walk(critical_sections[0])
        }
        owner_accesses = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "_owner"
        ]
        assert owner_accesses
        assert all(id(node) in inside for node in owner_accesses)
        assert any(isinstance(node.ctx, ast.Load) for node in owner_accesses)
        assert any(isinstance(node.ctx, ast.Store) for node in owner_accesses)

    async def test_a_guarded_hook_is_a_no_op_for_a_non_owner(self):
        gate = self._gate()
        owner, other = gate.new_window(), gate.new_window()
        owner.claim()
        ran = []

        async def close():
            ran.append("closed")

        await owner.guard(close)()
        assert ran == ["closed"]

        outcome = other.guard(close)()
        assert outcome is None, "a non-owner hook returned work to await"
        assert ran == ["closed"], "a non-owner closed another window's resource"

    def test_a_guarded_sync_hook_keeps_its_synchronous_shape(self):
        """`run_finalizers` awaits a hook result only when it is awaitable, so
        the wrapper must not turn a sync hook into a coroutine."""
        gate = self._gate()
        window = gate.new_window()
        window.claim()
        ran = []
        assert window.guard(lambda: ran.append("sync"))() is None
        assert ran == ["sync"]


class TestOneServingWindowPerProcess:
    """R7-F1: `LifespanGuard` gates PER INSTANCE, the resources are GLOBAL.

    Every `create_app()` builds its own guard with its own startup gate, but the
    consolidator singleton, the Graph Memory extractor/embedder registries and
    the shared inference runtime holder are process-wide. Two independently
    created applications therefore each held a valid-looking gate over one
    shared set of resources.

    The refusal lands in the dedicated synchronous ownership phase: after pure
    validation, before any resource may be acquired. Release is a positive
    post-cleanup checkpoint, not an exhaustive finalizer, so terminal
    uncertainty retains the slot.
    """

    @staticmethod
    def _drive(app):
        import uvicorn
        from uvicorn.lifespan.on import LifespanOn

        return LifespanOn(uvicorn.Config(app, lifespan="auto", log_config=None))

    @staticmethod
    def _core(monkeypatch, closes):
        from live_mem import server as core_server
        from live_mem.core import inference_runtime as core_runtime

        monkeypatch.setattr(core_runtime, "validate_inference_startup", lambda: None)
        monkeypatch.setattr(
            core_server,
            "_close_core_process_resources",
            lambda: closes.append("core-consolidator"),
        )
        monkeypatch.setattr(
            core_server,
            "_close_inference_runtime",
            lambda: closes.append("core-inference"),
        )
        return lambda: _core_app(monkeypatch, _RecordingInnerApp())

    @staticmethod
    def _graph(monkeypatch, closes):
        apply_graph_memory_baseline_env(monkeypatch)
        from mcp_memory import server as gm

        monkeypatch.setattr(gm, "_validate_inference_startup", lambda: None)
        monkeypatch.setattr(gm, "_initialize_graph_document_schema", lambda: None)
        monkeypatch.setattr(
            gm, "_close_llm_singletons", lambda: closes.append("graph-singletons")
        )
        monkeypatch.setattr(
            gm, "_close_inference_runtime", lambda: closes.append("graph-inference")
        )
        monkeypatch.setattr(
            gm.mcp, "streamable_http_app", lambda: _RecordingInnerApp()
        )
        return gm._create_app

    @pytest.fixture(params=["core", "graph"])
    def factory(self, request, monkeypatch):
        """A callable building a FRESH application, plus the close ledger.

        Parametrised over both services because the defect was a property of
        the wiring pattern, not of either service — pinning it once would leave
        the other free to regress, which is exactly how the four earlier
        ownership findings each survived in the copy they did not name.
        """
        closes: list[str] = []
        build = (self._core if request.param == "core" else self._graph)(
            monkeypatch, closes
        )
        return build, closes, request.param

    async def test_a_second_application_is_refused_and_touches_nothing(
        self, factory
    ):
        build, closes, _service = factory

        first = self._drive(build())
        await asyncio.wait_for(first.startup(), timeout=2.0)
        assert not first.startup_failed
        assert closes == []

        second = self._drive(build())
        await asyncio.wait_for(second.startup(), timeout=2.0)
        assert second.startup_failed, "a second application in one process started"
        assert second.should_exit

        # The whole point: the refusal ran NO shutdown hook, so the first
        # window still owns everything it acquired.
        assert closes == [], (
            "the refused application tore down the first window's resources"
        )

        # And the first window is still able to stop cleanly on its own terms.
        await asyncio.wait_for(first.shutdown(), timeout=2.0)
        assert not first.shutdown_failed
        assert len(closes) == 2, closes

    async def test_a_sequential_second_window_still_starts(self, factory):
        """Non-vacuity. The gate must refuse OVERLAP, not a restart — the
        holder is explicitly documented to support a second serving window in
        one process."""
        build, closes, _service = factory

        first = self._drive(build())
        await asyncio.wait_for(first.startup(), timeout=2.0)
        await asyncio.wait_for(first.shutdown(), timeout=2.0)
        assert not first.shutdown_failed

        second = self._drive(build())
        await asyncio.wait_for(second.startup(), timeout=2.0)
        assert not second.startup_failed, "the window was never released"
        await asyncio.wait_for(second.shutdown(), timeout=2.0)
        assert not second.shutdown_failed

    def test_two_thread_event_loops_admit_exactly_one_factory(
        self,
        factory,
        monkeypatch,
    ):
        """R8-F1 through the real Core/Graph factory and Uvicorn driver."""
        import queue
        import threading

        from hivemind_inference.process_window import ProcessWindow

        build, closes, _service = factory
        contenders_ready = threading.Barrier(2)
        release_winner = threading.Event()
        startup_outcomes = queue.Queue()
        completion_errors = queue.Queue()
        original_claim = ProcessWindow.claim

        def aligned_claim(window):
            contenders_ready.wait(timeout=2.0)
            return original_claim(window)

        monkeypatch.setattr(ProcessWindow, "claim", aligned_claim)
        apps = [build(), build()]

        def run(index, app):
            async def scenario():
                state = self._drive(app)
                try:
                    await asyncio.wait_for(state.startup(), timeout=2.0)
                    startup_outcomes.put((index, state.startup_failed, None))
                    if not state.startup_failed:
                        while not release_winner.is_set():
                            await asyncio.sleep(0.001)
                        await asyncio.wait_for(state.shutdown(), timeout=2.0)
                except BaseException as exc:  # noqa: BLE001 - thread evidence
                    if state.startup_event.is_set():
                        completion_errors.put((index, exc))
                    else:
                        startup_outcomes.put((index, None, exc))

            asyncio.run(scenario())

        threads = [
            threading.Thread(target=run, args=(index, app))
            for index, app in enumerate(apps)
        ]
        for thread in threads:
            thread.start()

        observed = [
            startup_outcomes.get(timeout=4.0),
            startup_outcomes.get(timeout=4.0),
        ]
        release_winner.set()
        for thread in threads:
            thread.join(timeout=4.0)
            assert not thread.is_alive()

        assert all(error is None for _, _, error in observed), observed
        assert completion_errors.empty(), list(completion_errors.queue)
        assert sorted(failed for _, failed, _ in observed) == [False, True]
        assert len(closes) == 2, "the refused contender ran cleanup hooks"

    @pytest.mark.parametrize("failure_kind", ("error", "cancelled"))
    async def test_failed_shutdown_keeps_the_process_window_claimed(
        self,
        factory,
        monkeypatch,
        failure_kind,
    ):
        """A terminal close failure requires recycle, not a fresh factory."""
        from live_mem import server as core_server
        from mcp_memory import server as gm

        build, closes, service = factory

        async def fail_close():
            closes.append(f"{service}-{failure_kind}")
            if failure_kind == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("process cleanup not confirmed")

        if service == "core":
            monkeypatch.setattr(
                core_server,
                "_close_core_process_resources",
                fail_close,
            )
        else:
            monkeypatch.setattr(gm, "_close_llm_singletons", fail_close)

        first = self._drive(build())
        await asyncio.wait_for(first.startup(), timeout=2.0)
        assert not first.startup_failed
        await asyncio.wait_for(first.shutdown(), timeout=2.0)
        assert first.shutdown_failed

        replacement = self._drive(build())
        await asyncio.wait_for(replacement.startup(), timeout=2.0)
        assert replacement.startup_failed
        assert replacement.should_exit

    async def test_a_rollback_after_acquiring_releases_the_window(
        self, factory, monkeypatch
    ):
        """The other half of the contract.

        A window that DID claim and then failed its resource startup runs the
        full shutdown bundle — correctly, because it may have acquired. It must
        release the window on the way out, or one failed startup would make the
        process permanently unstartable.
        """
        import live_mem.server as core_server
        from mcp_memory import server as gm

        build, closes, _service = factory
        attempts = []

        def _refuse_once():
            attempts.append(True)
            if len(attempts) == 1:
                raise InferenceConfigError("resource startup refused")

        # Patch both services' startup hook; only the one under test is built.
        for module in (core_server, gm):
            monkeypatch.setattr(module, "_validate_inference_startup", _refuse_once)

        failed = self._drive(build())
        await asyncio.wait_for(failed.startup(), timeout=2.0)
        assert failed.startup_failed
        assert closes, "the rollback skipped the cleanup this window does own"

        closes.clear()
        recovered = self._drive(build())
        await asyncio.wait_for(recovered.startup(), timeout=2.0)
        assert not recovered.startup_failed, (
            "a failed startup left the process window permanently claimed"
        )
        assert len(attempts) == 2
        await asyncio.wait_for(recovered.shutdown(), timeout=2.0)
        assert not recovered.shutdown_failed

    @pytest.mark.parametrize("failure_kind", ("error", "cancelled"))
    async def test_incomplete_startup_rollback_keeps_the_window_claimed(
        self,
        factory,
        monkeypatch,
        failure_kind,
    ):
        """A failed startup is reusable only when its rollback is complete."""
        import live_mem.server as core_server
        from mcp_memory import server as gm

        build, closes, service = factory
        startup_attempts = []

        def refuse_startup():
            startup_attempts.append(1)
            raise InferenceConfigError("resource startup refused")

        async def fail_rollback():
            closes.append(f"{service}-rollback-{failure_kind}")
            if failure_kind == "cancelled":
                raise asyncio.CancelledError()
            raise RuntimeError("startup rollback not confirmed")

        for module in (core_server, gm):
            monkeypatch.setattr(
                module,
                "_validate_inference_startup",
                refuse_startup,
            )
        if service == "core":
            monkeypatch.setattr(
                core_server,
                "_close_core_process_resources",
                fail_rollback,
            )
        else:
            monkeypatch.setattr(gm, "_close_llm_singletons", fail_rollback)

        failed = self._drive(build())
        await asyncio.wait_for(failed.startup(), timeout=2.0)
        assert failed.startup_failed
        assert startup_attempts == [1]

        replacement = self._drive(build())
        await asyncio.wait_for(replacement.startup(), timeout=2.0)
        assert replacement.startup_failed
        assert replacement.should_exit
        assert startup_attempts == [1], (
            "the replacement crossed the retained owner into resource startup"
        )



# --------------------------------------------------------------------------- #
# 5./6. Deployment purity: Compose, Dockerfile, public CI overlay              #
# --------------------------------------------------------------------------- #

class TestDeploymentPurity:
    @pytest.mark.parametrize(
        "relative",
        ["docs/DEPLOYMENT.md", "README.md", "README.fr.md"],
    )
    def test_operator_docs_scope_the_two_endpoint_requirement(self, relative):
        """PR #303 round 2 (Codex Sol, medium): the operator docs claimed
        unconditionally that "the provider" must expose both
        ``/chat/completions`` and ``/embeddings``.

        That is false for the split ``INFERENCE_*`` families this lot
        introduces, and false for the native ``anthropic`` chat profile, which
        speaks the Messages API. An operator following the canonical guide
        could reject a valid configuration or deploy incompatible endpoints.
        Every surviving statement of the joint requirement must therefore be
        scoped to the legacy unified path.
        """
        text = (_REPO_ROOT / relative).read_text(encoding="utf-8")
        paragraphs = [
            block
            for block in text.split("\n\n")
            if "/chat/completions" in block and "/embeddings" in block
        ]
        assert paragraphs, f"{relative} no longer states the requirement at all"
        # The claim must be bound to the LEGACY unified path specifically.
        # Merely mentioning the split families elsewhere in the paragraph is
        # not enough: a sentence reading "for every configuration, the provider
        # must expose both" is still false, and that is exactly the mutant this
        # assertion has to reject.
        legacy_scoping = ("legacy", "unifié", "unified")
        split_mention = ("INFERENCE_", "par rôle", "per role", "per ROLE")
        for block in paragraphs:
            # A table row naming the endpoint a given model id is accepted by
            # states no deployment-wide requirement.
            if block.lstrip().startswith("|"):
                continue
            assert any(token in block for token in legacy_scoping), block
            assert any(token in block for token in split_mention), block

    def test_compose_graph_memory_injects_no_inference_family(self):
        """Compose ``environment`` wins over ``env_file``: any inference name
        pinned at service level would be injected into EVERY deployment and
        refuse startup on the family-coexistence check as soon as an operator
        moves to the split ``INFERENCE_*`` families."""
        compose = yaml.safe_load(
            (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        service = compose["services"]["graph-memory"]
        keys = [
            entry.split("=", 1)[0] if isinstance(entry, str) else entry
            for entry in service.get("environment", [])
        ]
        offenders = [
            key
            for key in keys
            if key.startswith("LLMAAS_") or key.startswith("INFERENCE_")
        ]
        assert offenders == []
        # The shared .env stays the single configuration authority.
        assert service.get("env_file") == ".env"

    def test_compose_builds_graph_memory_from_the_repository_root(self):
        compose = yaml.safe_load(
            (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        build = compose["services"]["graph-memory"]["build"]
        assert build["context"] == "."
        assert build["dockerfile"] == "services/graph-memory/Dockerfile"

    def test_graph_memory_image_installs_the_shared_package(self):
        dockerfile = (
            _REPO_ROOT / "services/graph-memory/Dockerfile"
        ).read_text(encoding="utf-8")
        assert "COPY src/hivemind_inference/ ./hivemind_inference/" in dockerfile
        # Root-relative sources everywhere else too: a leftover directory-
        # relative COPY would break the root build context.
        for line in dockerfile.splitlines():
            stripped = line.strip()
            if not stripped.upper().startswith("COPY "):
                continue
            for source in stripped.split()[1:-1]:
                assert source.startswith(
                    ("services/graph-memory/", "src/hivemind_inference/")
                ), line

    def test_public_ci_overlay_builds_no_image(self):
        """Asserted against whichever tree this suite is running in.

        This file is exported to the public tree, where the overlay SOURCE
        does not exist — the exporter materializes it at
        ``.github/workflows/ci.yml``. Resolving both locations keeps one
        contract statement while covering both artifacts: the private overlay
        source, and (uniquely, since the private-only CI-split suite cannot
        reach it) the materialized workflow that actually runs in the public
        repository.

        TQ-11 (#349): the public overlay's `build` job (Hivemind/WAF/Graph
        Memory matrix, including the P13-1C repository-root build-context
        requirement this test used to assert) is gone — that coverage moved
        to the private canonical repository's exact-SHA
        pre-release-validate.yml (TQ-9, #347). Prove absence here too, since
        this is the only path that ever inspects the MATERIALIZED public
        workflow rather than just the pre-export staging source.
        """
        candidates = [
            _REPO_ROOT / "release/public-overlay/.github/workflows/ci.yml",
            _REPO_ROOT / ".github/workflows/ci.yml",
        ]
        present = [path for path in candidates if path.is_file()]
        assert len(present) == 1, [str(path) for path in present]
        raw = present[0].read_text(encoding="utf-8")
        workflow = yaml.safe_load(raw)
        assert set(workflow["jobs"]) == {"test", "test_python314_arm64"}
        assert "docker/build-push-action" not in raw

    # The matching guard for the OFFLINE export audit — which must resolve the
    # Dockerfile's COPY sources against the DECLARED build context rather than
    # the Dockerfile's own directory — lives with that audit's private suite,
    # because naming its module here would leak a private marker into the
    # exported public tree.


# --------------------------------------------------------------------------- #
# Consolidator: unconfigured chat role fails closed before any work            #
# --------------------------------------------------------------------------- #

class TestConsolidatorFailsClosed:
    async def test_absent_chat_role_refuses_before_touching_storage(
        self, monkeypatch
    ):
        """Fail-closed with zero side effects: no storage read, no network, no
        durable write — the operation reports the missing provider instead."""
        from live_mem.core import consolidator as consolidator_module
        from live_mem.core.consolidator import ConsolidatorService

        with core_inference_runtime(chat=False, embedding=False):
            service = ConsolidatorService()

        def _storage_must_not_be_used():
            raise AssertionError("storage was reached despite an absent chat role")

        monkeypatch.setattr(
            consolidator_module, "get_storage", _storage_must_not_be_used
        )

        async def _no_reservation(_space_id):
            return None

        monkeypatch.setattr(
            consolidator_module, "assert_space_not_reserved", _no_reservation
        )

        result = await service.consolidate("space-a", enforce_cooldown=False)
        assert result["status"] == "error"
        assert "INFERENCE_CHAT_" in result["message"]
        assert "LLMAAS_API_URL" in result["message"]

    def test_output_budget_can_only_lower_the_profile_ceiling(self):
        """The adapter rejects a request above the profile ceiling as
        ``invalid_request``; the consolidator therefore clamps every call site
        (surgical edits, dedup merge, compaction) rather than trusting them."""
        import asyncio

        from live_mem.core.consolidator import ConsolidatorService

        service = object.__new__(ConsolidatorService)
        service._max_tokens = 4096
        service._timeout = 60
        captured = {}

        class _Provider:
            async def complete(self, request):
                captured["max_output_tokens"] = request.max_output_tokens
                return None

        class _Runtime:
            def chat_provider(self):
                return _Provider()

        import live_mem.core.inference_runtime as core_runtime

        original = core_runtime.get_inference_runtime
        core_runtime.get_inference_runtime = lambda: _Runtime()
        try:
            asyncio.run(
                service._complete_chat([{"role": "user", "content": "x"}], 999_999)
            )
        finally:
            core_runtime.get_inference_runtime = original
        assert captured["max_output_tokens"] == 4096
