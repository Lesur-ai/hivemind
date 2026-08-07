# -*- coding: utf-8 -*-
"""Graph Memory singleton of the shared inference runtime (P13-1C, #276).

LOCAL MODIFICATION to the vendored Graph Memory runtime (see
``THIRD_PARTY_NOTICES.md``): the embedded service resolves the SAME role
profiles as the Hivemind core, through the repository-owned
``hivemind_inference`` package (the split ``INFERENCE_*`` families, or the
strict complete ``LLMAAS_*`` legacy pair), instead of its historical drifting
``LLMAAS_*`` view — which carried its own defaults (a different chat model,
60000 output tokens, temperature 1.0) and could silently disagree with the core
about what "the configured model" meant.

Provider SDK construction lives exclusively inside the registered adapters. The
owned transports (``PROXY_URL`` included — the P12-3 egress classification is
unchanged) are closed on the ASGI lifespan shutdown path.
"""

from __future__ import annotations

from hivemind_inference import InferenceRuntime, ResolvedEmbeddingProfile
from hivemind_inference.holder import InferenceRuntimeHolder

from ..config import get_settings

# The lifecycle state machine is shared with the Hivemind core
# (``hivemind_inference.holder``): this module previously carried a
# byte-identical copy of it, and four consecutive review rounds each fixed an
# ownership defect in whichever copy the finding named.
_holder = InferenceRuntimeHolder(
    service="Graph Memory",
    shutdown_message=(
        "the Graph Memory inference runtime has been shut down; no new "
        "provider transport may be built"
    ),
    proxy_url=lambda: get_settings().proxy_url,
)


def get_inference_runtime() -> InferenceRuntime:
    """Process-wide runtime snapshot (fail-closed resolution on first use).

    Raises ``InferenceRuntimeClosed`` once the service has begun shutting down,
    so a late ingestion worker fails its operation honestly instead of silently
    opening an unowned provider transport.
    """
    return _holder.get()


def validate_inference_startup() -> None:
    """Resolve the inference configuration fail-closed at service startup.

    See :meth:`hivemind_inference.holder.InferenceRuntimeHolder.validate_startup`
    for the contract: fail-closed resolution, the serving-window scope of the
    terminal flag, the refusal to start over a previous window's unreleased
    transports, and the publish-on-success ordering.
    """
    _holder.validate_startup()


async def close_inference_runtime_if_initialized() -> None:
    """ASGI-shutdown hook: close the owned adapter transports (idempotent)."""
    await _holder.close_if_initialized()


def resolved_vector_dimensions() -> int:
    """Exact Qdrant vector size of the RESOLVED embedding profile (P13-1C).

    The single authority for how wide a vector collection is created. It lives
    here, not in the vector store, because the answer is a property of the
    shared role profile — the legacy ``LLMAAS_EMBEDDING_DIMENSIONS`` setting
    keeps its default on a deployment configured through the split
    ``INFERENCE_EMBEDDING_*`` family, so reading it would size collections from
    a value nobody configured.

    Fails CLOSED when no embedding role is configured: a collection created at
    a default width is a silently wrong vector space, and every vector written
    into it would have to be rebuilt.
    """
    profile = get_inference_runtime().config.embedding
    if profile is None:
        raise RuntimeError(
            "no embedding provider is configured — refusing to create a "
            "Qdrant collection with an unconfigured vector size; set the "
            "INFERENCE_EMBEDDING_* family (or the legacy complete "
            "LLMAAS_API_URL + LLMAAS_API_KEY pair)"
        )
    return profile.expected_dimensions


def resolved_embedding_profile() -> ResolvedEmbeddingProfile:
    """Exact process-frozen embedding profile used by Qdrant identity guards.

    The return type is deliberately supplied by the shared inference package;
    no Graph Memory setting is reconstructed here. This keeps collection
    compatibility and provider calls on the same immutable profile snapshot.
    """
    profile = get_inference_runtime().config.embedding
    if profile is None:
        raise RuntimeError(
            "no embedding provider is configured — refusing to resolve "
            "Qdrant collection identity; set the INFERENCE_EMBEDDING_* family "
            "(or the legacy complete LLMAAS_API_URL + LLMAAS_API_KEY pair)"
        )
    return profile


def reset_inference_runtime_for_tests() -> None:
    """Drop the cached runtime AND lift the terminal shutdown flag so tests can
    re-resolve a patched environment.

    Transports are owned per runtime instance; a test that actually built an
    adapter should use ``close_inference_runtime_if_initialized`` instead, so
    the transport is released rather than orphaned.
    """
    _holder.reset_for_tests()
