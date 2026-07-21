# -*- coding: utf-8 -*-
"""
FakeGraphTransport — a deterministic, offline substitute for
:class:`live_mem.core.graph_bridge.GraphMemoryClient`.

It implements the SAME duck-typed surface the bridge uses — ``call_tool`` /
``call_tools_batch`` / ``__aenter__`` / ``__aexit__`` and the constructor shape
``(base_url, token, timeout=120.0, **kwargs)`` — but talks to NOTHING: no MCP,
no httpx, no boto3, no openai, no clock, no uuid. Every call is recorded and
every response is a canned dict keyed by tool name, so a test that drives the
real :class:`GraphBridgeService` + :class:`LongEngine` is fully deterministic
and reproducible.

Reused by P4-4 (this wave) and the downstream P4-5 / P4-7 / P4-8 / P4-9 suites.

Usage::

    factory = FakeGraphTransport.factory(responses={"memory_query": {...}})
    bridge = GraphBridgeService(client_factory=factory)
    engine = LongEngine(bridge=bridge)
    ...
    assert factory.instances[-1].tool_names() == ["memory_query"]

Responses
---------
A canned response can be:

- a ``dict`` — returned verbatim every time the tool is called;
- a ``list[dict]`` — consumed FIFO (one per call), last value reused once
  exhausted (deterministic, never raises on over-call);
- a ``callable(arguments) -> dict`` — computed from the call's arguments.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union

# A canned response is a fixed dict, a FIFO list of dicts, or a function of args.
Response = Union[dict, list, Callable[[dict], dict]]


@dataclass
class RecordedCall:
    """One recorded tool invocation.

    ``via`` is ``"call_tool"`` for a single call or ``"call_tools_batch"`` for a
    call issued inside a batch — lets tests assert the seam still batches.
    """

    tool_name: str
    arguments: dict
    via: str = "call_tool"


def _strict_document_delete(arguments: dict) -> dict:
    """Default ``document_delete`` mimicking the REAL GM contract (P7-8).

    The real tool (``services/graph-memory/src/mcp_memory/server.py``) is keyed
    by ``document_id`` (UUID) — a ``filename``-keyed call is a validation error,
    never a delete. The pre-P7-8 permissive default (``{"status": "ok"}`` for
    ANY args) masked exactly that latent bug in ``push()``; this callable keeps
    the fake non-vacuous so a filename-keyed delete can never pass again.
    """

    document_id = arguments.get("document_id")
    if not document_id:
        return {
            "status": "error",
            "message": (
                "document_delete requires document_id (GM contract); "
                f"got keys: {sorted(arguments.keys())}"
            ),
        }
    # Real GM returns "deleted" (or "partial_deleted"), not "ok".
    return {"status": "deleted", "document_id": document_id}


# Default canned responses, keyed by GM tool name. ``status == "ok"`` (or the
# tool's real success status) so the bridge's reshape branches
# (connect/push/status) take their happy paths. Values are intentionally
# minimal but shaped like the real GM payloads the bridge reads
# (``memories`` / ``documents`` / stats counts / ``ontologies``).
# ``document_delete`` is a STRICT callable (see above), not a permissive dict.
_DEFAULT_RESPONSES: dict[str, Response] = {
    "system_health": {"status": "ok"},
    "memory_list": {"status": "ok", "memories": []},
    "memory_create": {"status": "ok", "memory_id": "fake-mem"},
    "memory_stats": {
        "status": "ok",
        "document_count": 0,
        "entity_count": 0,
        "relation_count": 0,
        "top_entities": [],
    },
    "document_list": {"status": "ok", "documents": []},
    "document_delete": _strict_document_delete,
    "memory_ingest": {"status": "ok", "ingested": True},
    "memory_search": {"status": "ok", "results": []},
    "memory_query": {"status": "ok", "results": []},
    "ontology_list": {"status": "ok", "ontologies": []},
}


class FakeGraphTransport:
    """Drop-in fake for ``GraphMemoryClient`` (no network, fully deterministic)."""

    def __init__(
        self,
        base_url: str = "http://gm.test",
        token: str = "",
        timeout: float = 120.0,
        *,
        responses: Optional[dict[str, Response]] = None,
        default: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        # Mirror GraphMemoryClient's URL normalization so url-derived asserts and
        # any future routing-by-url logic behave identically.
        normalized = base_url.rstrip("/")
        for suffix in ("/sse", "/mcp"):
            if normalized.endswith(suffix):
                normalized = normalized[: -len(suffix)]
        self.base_url = normalized
        self.token = token
        self.timeout = timeout
        # Capture any extra kwargs (e.g. a future param) without exploding — the
        # push/ingest path passes timeout positionally-as-kwarg; this also keeps
        # the fake forward-compatible.
        self.extra_kwargs = dict(kwargs)

        # Per-tool canned responses overlaid on the deterministic defaults.
        # Canned responses are DEEP-copied PER INSTANCE: the real bridge builds a
        # fresh client per method call, so a shared list (FIFO) would drain
        # globally across instances, and a shared dict (e.g. document_list's
        # nested `documents` list) could be mutated by a caller and leak across
        # instances/tests — order-dependent false-greens. Deep copy isolates both.
        # Callables pass through unchanged (they are pure factories).
        def _iso(v: "Response") -> "Response":
            return deepcopy(v) if isinstance(v, (list, dict)) else v

        self._responses: dict[str, Response] = {
            k: _iso(v) for k, v in _DEFAULT_RESPONSES.items()
        }
        if responses:
            self._responses.update({k: _iso(v) for k, v in responses.items()})
        self._default = default if default is not None else {"status": "ok"}

        # Recorded calls (order-preserving).
        self.calls: list[RecordedCall] = []

    # ── Response resolution ────────────────────────────────────────────────

    def _resolve(self, tool_name: str, arguments: dict) -> dict:
        spec = self._responses.get(tool_name, self._default)
        if callable(spec):
            return spec(arguments)
        if isinstance(spec, list):
            if not spec:
                return deepcopy(self._default)
            # FIFO: pop while more than one remains; reuse the last forever
            # (deterministic, never raises on over-call).
            if len(spec) > 1:
                return deepcopy(spec.pop(0))
            return deepcopy(spec[0])
        # Plain dict — return a DEEP COPY so a caller can never mutate the canned
        # response (a shared nested list like document_list's `documents` would
        # otherwise leak across instances/tests → order-dependent false-greens).
        return deepcopy(spec)

    # ── Transport surface (duck-typed to GraphMemoryClient) ────────────────

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        self.calls.append(
            RecordedCall(tool_name=tool_name, arguments=dict(arguments), via="call_tool")
        )
        return self._resolve(tool_name, arguments)

    async def call_tools_batch(self, calls: list[tuple[str, dict]]) -> list[dict]:
        results: list[dict] = []
        for tool_name, arguments in calls:
            self.calls.append(
                RecordedCall(
                    tool_name=tool_name,
                    arguments=dict(arguments),
                    via="call_tools_batch",
                )
            )
            results.append(self._resolve(tool_name, arguments))
        return results

    async def __aenter__(self) -> "FakeGraphTransport":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        return None

    # ── Test-facing inspection helpers ─────────────────────────────────────

    def tool_names(self) -> list[str]:
        """Ordered list of tool names across all recorded calls."""
        return [c.tool_name for c in self.calls]

    def args_for(self, name: str) -> list[dict]:
        """All recorded argument dicts for ``name`` (call order preserved)."""
        return [c.arguments for c in self.calls if c.tool_name == name]

    # ── Factory matching the GraphBridgeService client_factory shape ───────

    @staticmethod
    def factory(**default_kwargs: Any) -> "_FakeFactory":
        """Return a ``(url, token, **kw) -> FakeGraphTransport`` callable.

        This is the EXACT shape ``GraphBridgeService(client_factory=...)``
        expects. Every built instance is stashed on ``.instances`` so SSRF tests
        can assert ``factory.instances == []`` (zero clients built when a URL is
        refused before construction), and timeout tests can read
        ``factory.instances[-1].timeout``.

        ``default_kwargs`` (e.g. ``responses=...``) are applied to every built
        instance; per-call ``**kw`` from the bridge (notably ``timeout=180.0``
        on the ingest/push path) are forwarded too and win on conflict.
        """
        return _FakeFactory(default_kwargs)


class _FakeFactory:
    """Callable client-factory that records every instance it builds."""

    def __init__(self, default_kwargs: dict) -> None:
        self._default_kwargs = default_kwargs
        self.instances: list[FakeGraphTransport] = []

    def __call__(
        self, url: str, token: str, **kwargs: Any
    ) -> FakeGraphTransport:
        merged = dict(self._default_kwargs)
        merged.update(kwargs)  # per-call kwargs (e.g. timeout=180.0) win
        inst = FakeGraphTransport(url, token, **merged)
        self.instances.append(inst)
        return inst
