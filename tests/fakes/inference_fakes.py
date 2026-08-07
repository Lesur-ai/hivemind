# -*- coding: utf-8 -*-
"""Shared inference-boundary fixtures for consumer tests (P13-1C / #276).

Both consuming services resolve their role profiles ONCE per process through
``hivemind_inference`` and hold the result in a module-level runtime singleton.
A test that exercises a migrated consumer therefore needs a *resolved* runtime,
not a patched SDK constructor — that is what these helpers install.

Nothing here builds a transport: the returned runtime holds resolved profiles
only, and an adapter is constructed lazily on first use. A test that wants a
real adapter points the profile endpoint at
``tests/fakes/inference_emulator.InferenceEmulator`` instead.

Both context managers restore the previous singleton, so a suite that installs
a runtime cannot leak one into an unrelated test.
"""

from __future__ import annotations

import asyncio
import contextlib

from hivemind_inference import (
    InferenceConfig,
    InferenceRuntime,
    ResolvedChatProfile,
    ResolvedEmbeddingProfile,
)

# Generic explicit endpoint: the ``openai-compatible`` profile is the only one
# that accepts an arbitrary operator endpoint, and ``.invalid`` (RFC 2606) can
# never resolve, so an accidental real connection cannot succeed.
DEFAULT_ENDPOINT = "http://provider.p13-1c.invalid/v1"


def make_chat_profile(**overrides) -> ResolvedChatProfile:
    fields = {
        "provider_id": "openai-compatible",
        "adapter_id": "openai-compatible",
        "endpoint": DEFAULT_ENDPOINT,
        "api_key": "test-key",
        "configured_model": "test-chat-model",
        "context_window": 131072,
        "max_output_tokens": 16384,
        "temperature": 0.3,
        "source": "inference",
    }
    fields.update(overrides)
    return ResolvedChatProfile(**fields)


def make_embedding_profile(**overrides) -> ResolvedEmbeddingProfile:
    fields = {
        "provider_id": "openai-compatible",
        "adapter_id": "openai-compatible",
        "endpoint": DEFAULT_ENDPOINT,
        "api_key": "test-key",
        "configured_model": "test-embedding-model",
        "expected_dimensions": 1024,
        "source": "inference",
    }
    fields.update(overrides)
    return ResolvedEmbeddingProfile(**fields)


def make_inference_config(
    *,
    chat: ResolvedChatProfile | None | bool = True,
    embedding: ResolvedEmbeddingProfile | None | bool = False,
    legacy_active: bool = False,
) -> InferenceConfig:
    """``True`` means "the default profile for that role"; ``False``/``None``
    means the role is not configured at all."""
    if chat is True:
        chat = make_chat_profile()
    if embedding is True:
        embedding = make_embedding_profile()
    return InferenceConfig(
        chat=chat or None,
        embedding=embedding or None,
        legacy_active=legacy_active,
    )


def make_runtime(*, proxy_url: str | None = None, **config_kwargs) -> InferenceRuntime:
    return InferenceRuntime(
        make_inference_config(**config_kwargs), proxy_url=proxy_url
    )


class LifecycleAdapter:
    """Deterministic adapter double for close-lifecycle tests.

    ``aclose()`` announces itself on :attr:`entered` and then blocks on
    :attr:`release`, so a test synchronises on FACTS — "the close has started",
    "the close may now finish" — instead of on a sleep long enough to probably
    work. Sleep-based lifecycle tests are how this suite previously ended up
    asserting properties it never actually exercised: the timing hid the
    ordering that mattered.

    Modes: ``ok`` returns after release, ``raise`` raises after release, and
    ``hang`` never returns at all (for the shutdown-budget path). A test that
    wants an immediate close simply sets :attr:`release` before the close
    starts.
    """

    __slots__ = ("mode", "error", "entered", "release", "calls")

    def __init__(self, *, mode: str = "ok", error: BaseException | None = None) -> None:
        assert mode in {"ok", "raise", "hang"}
        self.mode = mode
        self.error = error or RuntimeError("transport close failed")
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def aclose(self) -> None:
        self.calls += 1
        self.entered.set()
        if self.mode == "hang":
            await asyncio.Event().wait()  # never set by anyone
        await self.release.wait()
        if self.mode == "raise":
            raise self.error


@contextlib.contextmanager
def _installed_runtime(holder, runtime):
    """Install ``runtime`` in ``holder`` and restore the holder's whole state.

    The state is captured through the holder's own opaque snapshot rather than
    field by field. A fixture that names the fields has to be updated every
    time the lifecycle grows one, and forgetting is silent: the leaked field
    poisons unrelated tests in whatever order they happen to run. This is the
    fixture-side half of the same ownership discipline the holder enforces.
    """
    snapshot = holder.snapshot_for_tests()
    holder.restore_for_tests((runtime, False))
    try:
        yield runtime
    finally:
        holder.restore_for_tests(snapshot)


@contextlib.contextmanager
def core_inference_runtime(*, proxy_url: str | None = None, **config_kwargs):
    """Install a resolved runtime as the Hivemind-core singleton."""
    from live_mem.core import inference_runtime as core_runtime

    runtime = make_runtime(proxy_url=proxy_url, **config_kwargs)
    with _installed_runtime(core_runtime._holder, runtime):
        yield runtime


def apply_graph_memory_baseline_env(monkeypatch) -> None:
    """Minimum env for importing any ``mcp_memory`` module.

    ``mcp_memory/config.py`` builds ``Settings()`` at MODULE level and several
    credential fields are required, so a bare import raises without this. Kept
    here so every suite that reaches a Graph Memory module states the same
    baseline instead of re-inventing one.
    """
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://s3.test.invalid:9000")
    monkeypatch.setenv("S3_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY", "test-secret-key")
    monkeypatch.setenv("S3_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("S3_REGION_NAME", "fr1")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-neo4j-password")


@contextlib.contextmanager
def gm_inference_runtime(*, proxy_url: str | None = None, **config_kwargs):
    """Install a resolved runtime as the embedded Graph Memory singleton."""
    from mcp_memory.core import inference_runtime as gm_runtime

    runtime = make_runtime(proxy_url=proxy_url, **config_kwargs)
    with _installed_runtime(gm_runtime._holder, runtime):
        yield runtime
