# -*- coding: utf-8 -*-
"""Hivemind-core singleton of the shared inference runtime (P13-1C, #276).

The core resolves its role profiles ONCE per process through
``hivemind_inference`` (raw-presence selection: the split ``INFERENCE_*``
families, or the strict complete ``LLMAAS_*`` 1.x legacy pair) and owns the
adapter transports here. Provider SDK construction lives exclusively inside the
registered adapters — this module and every consumer hold only the normalized
contracts.

Health integration: ONE total (never-raising) builder produces the
``services.llmaas`` block for both the public ``GET /health`` and the
authenticated ``system_health``, so the two surfaces cannot drift. The
historical top-level fields are preserved exactly and the additive
``chat``/``embedding`` role children follow ADR-0027:

- the public block stays ANONYMOUS — no provider, adapter, model, dimension,
  endpoint, fingerprint, or error category below the historical fields;
- neither surface performs generation or embedding: both roles are probed
  discovery-only, so a health call spends zero provider tokens (HM-12);
- authenticated health may read fresh matching evidence produced by the
  manage-only ``inference_self_test``; public health ignores that cache, and
  neither surface ever refreshes it.
"""

from __future__ import annotations

import asyncio
import logging

from hivemind_inference import InferenceConfigError, InferenceRuntime
from hivemind_inference.certification_budget import (
    protected_certification_graph_health_timeout_seconds,
)
from hivemind_inference.holder import InferenceRuntimeHolder
from hivemind_inference.records import ProbeResult
from hivemind_inference.runtime import InferenceRuntimeClosed

from ..config import get_settings

logger = logging.getLogger("live_mem.inference")

# Historical per-probe timeout shared by /health and system_health (P12-1).
PROBE_TIMEOUT_SECONDS = 5

# The lifecycle state machine is shared with the embedded Graph Memory runtime
# (``hivemind_inference.holder``). The two services previously carried
# byte-identical copies of it, and four consecutive review rounds each fixed an
# ownership defect in whichever copy the finding named.
_holder = InferenceRuntimeHolder(
    service="Hivemind",
    shutdown_message=(
        "the Hivemind inference runtime has been shut down; no new "
        "provider transport may be built"
    ),
    proxy_url=lambda: get_settings().proxy_url,
)


def get_inference_runtime() -> InferenceRuntime:
    """Process-wide runtime snapshot (fail-closed resolution on first use).

    Raises ``InferenceRuntimeClosed`` once the service has begun shutting down,
    so a late background task fails its operation honestly instead of silently
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
    from .inference_readiness import open_inference_self_test_window

    open_inference_self_test_window()


async def close_inference_runtime_if_initialized() -> None:
    """Drain deep-readiness work, then close owned adapter transports."""
    from hivemind_inference.asgi_lifespan import run_finalizers

    from .inference_readiness import close_inference_self_test_window

    failure = await run_finalizers(
        close_inference_self_test_window,
        _holder.close_if_initialized,
    )
    if failure is not None:
        raise failure


def reset_inference_runtime_for_tests() -> None:
    """Drop the cached runtime AND lift the terminal shutdown flag so tests can
    re-resolve a patched environment.

    Transports are owned per runtime instance; a test that actually built an
    adapter should use ``close_inference_runtime_if_initialized`` instead, so
    the transport is released rather than orphaned.
    """
    from .inference_readiness import reset_inference_self_test_for_tests

    reset_inference_self_test_for_tests()
    _holder.reset_for_tests()


async def run_inference_self_test() -> dict:
    """Run the bounded deep check against this process's frozen profiles."""
    from .inference_readiness import run_inference_self_test as _run

    return await _run(get_inference_runtime())


# ──────────────────────────────────────────────────────────────────────────
# Health block builder (shared by /health and system_health)
# ──────────────────────────────────────────────────────────────────────────

# ADR-0027 role child for a role that is not configured at all. `connectivity`
# uses the dedicated `not_configured` value (never `unreachable`, which would
# read as an outage).
_NOT_CONFIGURED_CHILD = {
    "status": "warning",
    "configured": False,
    "connectivity": "not_configured",
    "discovery": "not_run",
    "model_available": None,
    "readiness": "unknown",
    "evidence": "none",
}


def _child_from_probe(result: ProbeResult) -> dict:
    """Anonymous ADR-0027 role child derived from a discovery probe."""
    if result.discovery == "available":
        evidence = "discovery"
    elif result.connectivity == "reachable":
        evidence = "connectivity"
    else:
        evidence = "none"
    child = {
        # `healthy` is reachable AND (listing available OR listing simply not
        # offered): an endpoint that answers but does not implement /models is
        # NOT a provider failure (ADR-0027; credit @sylvainkalache, public
        # PR #11).
        "status": "ok" if result.healthy else "error",
        "configured": True,
        "connectivity": result.connectivity,
        "discovery": result.discovery,
        "model_available": result.model_available,
        # Health performs no generation and no embedding, so it can never
        # observe readiness.
        "readiness": "unknown",
        "evidence": evidence,
    }
    if result.latency_ms is not None:
        child["latency_ms"] = result.latency_ms
    return child


async def _probe_role(runtime: InferenceRuntime, role: str) -> ProbeResult | None:
    """Probe one role, or ``None`` when that role is not configured."""
    if role == "chat":
        if runtime.config.chat is None:
            return None
        probe = runtime.chat_probe()
    else:
        if runtime.config.embedding is None:
            return None
        probe = runtime.embedding_probe()
    return await probe.probe(timeout_seconds=PROBE_TIMEOUT_SECONDS)


async def _probe_role_safely(
    runtime: InferenceRuntime, role: str
) -> ProbeResult | None:
    """``_probe_role`` that converts an adapter-construction or unexpected
    failure into an ``unreachable`` probe result instead of propagating.

    Building an adapter can fail synchronously (transport construction), and
    ADR-0027 forbids surfacing a raw provider/transport exception on a health
    surface. Returning a normalized error result keeps the two roles
    independent: a broken embedding transport must not blank out the chat
    block, and vice versa.
    """
    try:
        return await _probe_role(runtime, role)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - health is total by contract
        logger.warning("health: %s role probe failed: %s", role, type(exc).__name__)
        return ProbeResult(
            connectivity="unreachable",
            discovery="error",
            model_available=None,
            error_category="unavailable",
        )


async def build_llmaas_health_block(*, authenticated: bool) -> dict:
    """Complete ``services.llmaas`` value for the calling health surface.

    Historical top-level fields are preserved exactly:

    - ``{"status": "ok", "latency_ms": ...}`` (public), plus ``model`` and
      ``model_available`` (authenticated), when the CHAT role answers;
    - ``{"status": "warning", "message": "LLMaaS non configuré"}`` when the
      chat role is not configured;
    - ``{"status": "error", "message": "LLMaaS unreachable"}`` on failure.

    The additive ``chat``/``embedding`` children carry the anonymous ADR-0027
    fields on every surface, and safe provider identity only when
    ``authenticated``. Never raises.
    """
    try:
        runtime = get_inference_runtime()
    except (InferenceConfigError, InferenceRuntimeClosed) as exc:
        # Startup would have refused an invalid environment, and a shutting-down
        # service has no runtime to probe. Either way the HISTORICAL top-level
        # error envelope is preserved byte-for-byte — health field compatibility
        # is an acceptance criterion of this lot, and a new top-level message
        # would break a consumer parsing the documented shape. The specific
        # cause stays in the server log and, on the AUTHENTICATED surface only,
        # in the additive role children.
        logger.warning(
            "health: inference runtime unavailable (%s): %s",
            type(exc).__name__,
            exc,
        )
        block: dict = {"status": "error", "message": "LLMaaS unreachable"}
        for role in ("chat", "embedding"):
            child = dict(_NOT_CONFIGURED_CHILD)
            child["status"] = "error"
            if authenticated:
                child["error_category"] = (
                    "invalid_request"
                    if isinstance(exc, InferenceConfigError)
                    else "unavailable"
                )
            block[role] = child
        return block

    chat_result, embedding_result = await asyncio.gather(
        _probe_role_safely(runtime, "chat"),
        _probe_role_safely(runtime, "embedding"),
    )

    if chat_result is None:
        block: dict = {"status": "warning", "message": "LLMaaS non configuré"}
    elif chat_result.healthy:
        block = {"status": "ok"}
        if authenticated:
            # HM-18: the configured model name is fingerprinting on an
            # anonymous endpoint; it stays on the authenticated surface only.
            block["model"] = runtime.config.chat.configured_model
            block["model_available"] = chat_result.model_available
        if chat_result.latency_ms is not None:
            block["latency_ms"] = chat_result.latency_ms
    else:
        block = {"status": "error", "message": "LLMaaS unreachable"}

    chat_child = (
        dict(_NOT_CONFIGURED_CHILD)
        if chat_result is None
        else _child_from_probe(chat_result)
    )
    embedding_child = (
        dict(_NOT_CONFIGURED_CHILD)
        if embedding_result is None
        else _child_from_probe(embedding_result)
    )
    if authenticated:
        if runtime.config.chat is not None:
            snapshot = runtime.config.chat.safe_snapshot()
            chat_child["provider_id"] = snapshot["provider_id"]
            chat_child["adapter_id"] = snapshot["adapter_id"]
            chat_child["configured_model"] = snapshot["configured_model"]
            if chat_result is not None and chat_result.error_category is not None:
                chat_child["error_category"] = chat_result.error_category
        if runtime.config.embedding is not None:
            snapshot = runtime.config.embedding.safe_snapshot()
            embedding_child["provider_id"] = snapshot["provider_id"]
            embedding_child["adapter_id"] = snapshot["adapter_id"]
            embedding_child["configured_model"] = snapshot["configured_model"]
            embedding_child["expected_dimensions"] = snapshot["expected_dimensions"]
            if (
                embedding_result is not None
                and embedding_result.error_category is not None
            ):
                embedding_child["error_category"] = embedding_result.error_category
        # Authenticated health is a cache READER only. The matching function
        # performs no provider operation and returns evidence only when the
        # exact combined frozen role-profile fingerprint is still fresh.
        from .inference_readiness import read_fresh_inference_self_test

        cached = read_fresh_inference_self_test(runtime)
        if cached is not None:
            for child, evidence in (
                (chat_child, cached.chat),
                (embedding_child, cached.embedding),
            ):
                child["readiness"] = evidence.readiness
                child["evidence"] = "inference" if evidence.configured else "none"
                if evidence.configured and evidence.readiness == "not_ready":
                    # A fresh paid probe is stronger than discovery-only
                    # reachability. Only the additive role child is degraded;
                    # historical/public top-level health remains unchanged.
                    child["status"] = "error"
                child["checked_at"] = cached.checked_at
                child["expires_at"] = cached.expires_at
                if evidence.resolved_model is not None:
                    child["resolved_model"] = evidence.resolved_model
                if evidence.model_evidence is not None:
                    child["model_evidence"] = evidence.model_evidence
                if evidence.error_category is not None:
                    child["error_category"] = evidence.error_category
    block["chat"] = chat_child
    block["embedding"] = embedding_child
    return block
