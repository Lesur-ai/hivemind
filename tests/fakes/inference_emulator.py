# -*- coding: utf-8 -*-
"""Deterministic provider emulator for the P13 inference boundary (#275).

One in-process ``asyncio.start_server`` HTTP/1.1 listener (``Connection:
close`` per request, ephemeral 127.0.0.1 port, ZERO real network and ZERO
credentials) emulating BOTH frozen ADR-0027 wire shapes:

- **OpenAI-compatible**: ``GET .../models``, ``POST .../chat/completions``,
  ``POST .../embeddings`` (path suffix matched, so bases with or without
  ``/v1`` both work — the Cloud Temple regression shape includes ``/v1``);
- **native Anthropic**: ``POST /v1/messages``, ``GET /v1/models``.

A real socket listener (rather than an ``httpx`` transport mock) is required
here: proving proxy routing and the pre-send/post-send failure distinction
that ADR-0027's retry contract depends on needs a genuine connection — a
mocked client-side transport cannot represent "the proxy received an
absolute-form request" or "the connection was refused before any byte was
sent".

Extends the P12-3 ``_HttpEndpoint`` pattern
(``tests/test_p12_3_gm_proxy_runtime.py``) with header capture and
per-request scripting so adapter tests can assert the exact wire contract
(headers, body fields, temperature omission) and drive every failure family:
auth, 429 with/without/oversized ``Retry-After``, quota codes, 5xx, stalls
(timeout), post-send connection drops, malformed/empty/truncated JSON, wrong
embedding cardinality/dimension/non-finite values, refusals, and unsupported
``/models`` discovery.

Script entries are consumed in request order; each is ``None`` (canned
success) or a dict with any of:

- ``status``: integer HTTP status (default 200);
- ``body``: JSON-serializable payload (default: canned for the path);
- ``body_raw``: exact bytes to send instead of JSON;
- ``headers``: extra response headers, e.g. ``{"retry-after": "3"}``;
- ``action``: ``"stall"`` (never answer) or ``"drop"`` (read the request,
  then close the connection without any response bytes — the ambiguous
  post-send outcome).

The emulator also works as an HTTP *proxy* target (absolute-form request
lines are recorded verbatim and answered locally; ``CONNECT`` tunnels are
recorded and refused, exactly like the P12-3 fake), so proxy-routing and
direct-connection-trap proofs reuse it unchanged.

Reuse note: this module is extracted and adapted from the draft PR #273 slice
(materially authored by ``claude-fable-5``, Anthropic) on branch
``claude/p13-1-implementation-425762``. It has no dependency on
``hivemind_inference`` internals, so it ports unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import struct

_REASONS = {
    200: b"200 OK",
    400: b"400 Bad Request",
    401: b"401 Unauthorized",
    403: b"403 Forbidden",
    404: b"404 Not Found",
    405: b"405 Method Not Allowed",
    407: b"407 Proxy Authentication Required",
    413: b"413 Content Too Large",
    422: b"422 Unprocessable Entity",
    429: b"429 Too Many Requests",
    500: b"500 Internal Server Error",
    501: b"501 Not Implemented",
    502: b"502 Bad Gateway",
    503: b"503 Service Unavailable",
    529: b"529 Overloaded",
}


def openai_chat_payload(
    text: str = "canned completion",
    *,
    model: str = "emulated-chat-model",
    finish_reason: str = "stop",
    usage: dict | None = None,
    refusal: str | None = None,
):
    message: dict = {"role": "assistant", "content": text}
    if refusal is not None:
        message["refusal"] = refusal
    return {
        "id": "chatcmpl-p13",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason}
        ],
        "usage": usage
        if usage is not None
        else {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12},
    }


def openai_embeddings_payload(
    count: int,
    *,
    dimensions: int = 4,
    model: str = "emulated-embedding-model",
    base: float = 0.25,
    encoding_format: str = "float",
):
    """Canned embeddings body in the requested wire encoding.

    ``encoding_format`` is honored rather than ignored: a real
    OpenAI-compatible server returns base64-packed little-endian float32 when
    the request asks for ``base64``, and the OpenAI SDK injects that value
    whenever the caller omits the parameter. An emulator that always returned
    float arrays would silently hide an adapter that fails to pin the
    encoding it can actually parse.
    """
    vectors = [[base + i] * dimensions for i in range(count)]
    if encoding_format == "base64":
        # Serialize EXPLICITLY little-endian ("<"), never array.array's native
        # host order. A native-order emulator would byte-swap in lockstep with
        # a native-order decoder, making the pair self-consistent and blind to
        # a real big-endian portability failure — the test could not fail on
        # the very bug it is supposed to guard.
        encoded = [
            base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")
            for vector in vectors
        ]
    else:
        encoded = vectors
    return {
        "object": "list",
        "data": [
            {
                "object": "embedding",
                "index": i,
                "embedding": encoded[i],
            }
            for i in range(count)
        ],
        "model": model,
        "usage": {"prompt_tokens": 3, "total_tokens": 3},
    }


def openai_models_payload(model_ids: tuple[str, ...] = ("emulated-chat-model",)):
    return {
        "object": "list",
        "data": [{"id": model_id, "object": "model"} for model_id in model_ids],
    }


def anthropic_message_payload(
    text: str = "canned anthropic completion",
    *,
    model: str = "emulated-anthropic-model",
    stop_reason: str = "end_turn",
    usage: dict | None = None,
):
    return {
        "id": "msg_p13",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage
        if usage is not None
        else {"input_tokens": 9, "output_tokens": 4},
    }


def anthropic_models_payload(
    model_ids: tuple[str, ...] = ("emulated-anthropic-model",),
):
    return {
        "data": [{"type": "model", "id": model_id} for model_id in model_ids],
        "has_more": False,
    }


class InferenceEmulator:
    """Deterministic dual-shape provider emulator (see module docstring)."""

    def __init__(
        self,
        scripted=None,
        *,
        embedding_dimensions: int = 4,
        rejected_fields: tuple[str, ...] = (),
    ):
        """``rejected_fields`` models a provider that REFUSES an unsupported
        request field with a 400 — e.g. Scaleway, whose Embeddings API
        documents ``encoding_format`` as unsupported. Without this mode the
        emulator silently accepts any field, which is precisely how a
        universally-pinned parameter passed review while being incompatible
        with a frozen reference profile.
        """
        self.requests: list[dict] = []
        self.connections = 0
        self.scripted = list(scripted or [])
        self.embedding_dimensions = embedding_dimensions
        self.rejected_fields = tuple(rejected_fields)
        self._server = None
        self.port: int | None = None
        self._stall = asyncio.Event()

    async def __aenter__(self) -> "InferenceEmulator":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc) -> None:
        self._stall.set()
        self._server.close()
        with contextlib.suppress(Exception):
            await self._server.wait_closed()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def v1_url(self) -> str:
        return f"{self.url}/v1"

    def _canned(self, method: str, target: str, body: bytes):
        path = target.split("?", 1)[0]
        if self.rejected_fields and body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                offending = sorted(set(parsed) & set(self.rejected_fields))
                if offending:
                    return 400, {
                        "error": {
                            "message": (
                                "unsupported request field(s): "
                                + ", ".join(offending)
                            ),
                            "type": "invalid_request_error",
                        }
                    }
        if path.endswith("/chat/completions") and method == "POST":
            return 200, openai_chat_payload()
        if path.endswith("/embeddings") and method == "POST":
            encoding_format = "float"
            try:
                parsed = json.loads(body or b"{}")
                raw_input = parsed.get("input", [])
                count = len(raw_input) if isinstance(raw_input, list) else 1
                # Honor the REQUESTED encoding, like a real provider. The
                # OpenAI SDK injects "base64" when the caller omits the
                # parameter, so an adapter that does not pin "float" will
                # receive base64 here and must be seen to fail.
                requested = parsed.get("encoding_format")
                if isinstance(requested, str):
                    encoding_format = requested
            except ValueError:
                count = 1
            return 200, openai_embeddings_payload(
                max(count, 1),
                dimensions=self.embedding_dimensions,
                encoding_format=encoding_format,
            )
        if path.endswith("/messages") and method == "POST":
            return 200, anthropic_message_payload()
        if path.endswith("/models") and method == "GET":
            # One canned listing serves both wire shapes: the OpenAI SDK reads
            # ``data[].id`` and the native probe reads the same field.
            return 200, {
                "object": "list",
                "data": [
                    {"id": "emulated-chat-model", "object": "model"},
                    {"id": "emulated-embedding-model", "object": "model"},
                    {"id": "emulated-anthropic-model", "type": "model"},
                ],
                "has_more": False,
            }
        return 404, {"error": {"message": "unknown path", "type": "not_found_error"}}

    async def _handle(self, reader, writer) -> None:
        self.connections += 1
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").split(" ")
            method, target = parts[0], parts[1]
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                headers[name.strip().lower()] = value.strip()
            body = b""
            length = int(headers.get("content-length", "0") or "0")
            if length:
                body = await reader.readexactly(length)
            record = {
                "method": method,
                "url": target,
                "headers": headers,
                "body": body,
            }
            try:
                record["json"] = json.loads(body) if body else None
            except ValueError:
                record["json"] = None
            self.requests.append(record)
            if method == "CONNECT":
                # https-origin tunnel through the emulator-as-proxy: record
                # and refuse (no TLS stack) — fail-closed, like P12-3.
                writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\n"
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return
            action = self.scripted.pop(0) if self.scripted else None
            if isinstance(action, dict) and action.get("action") == "stall":
                await self._stall.wait()
                return
            if isinstance(action, dict) and action.get("action") == "drop":
                return
            if isinstance(action, dict) and action.get("action") == "drip":
                # A SLOW-DRIP provider: headers promise a large body, then
                # bytes trickle in forever with a gap always shorter than the
                # client's read timeout. An httpx timeout bounds only
                # INACTIVITY between chunks, so it never fires — only a TOTAL
                # wall-clock deadline can stop this. A "stall" (silence) does
                # trip the read timeout and therefore cannot prove that bound.
                interval = float(action.get("interval", 0.02))
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: 1000000000\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                while True:
                    writer.write(b" ")
                    await writer.drain()
                    await asyncio.sleep(interval)
            status = 200
            payload = None
            raw = None
            extra_headers: dict[str, str] = {}
            if isinstance(action, dict):
                status = int(action.get("status", 200))
                extra_headers = dict(action.get("headers", {}))
                if "body_raw" in action:
                    raw = action["body_raw"]
                elif "body" in action:
                    payload = action["body"]
            if raw is None and payload is None:
                canned_status, payload = self._canned(method, target, body)
                if not isinstance(action, dict) or "status" not in action:
                    status = canned_status
            data = raw if raw is not None else json.dumps(payload).encode()
            reason = _REASONS.get(status, str(status).encode() + b" Emulated")
            header_lines = [
                b"HTTP/1.1 " + reason,
                b"Content-Type: application/json",
                b"Content-Length: " + str(len(data)).encode(),
                b"Connection: close",
            ]
            for name, value in extra_headers.items():
                header_lines.append(
                    name.encode("latin-1") + b": " + str(value).encode("latin-1")
                )
            writer.write(b"\r\n".join(header_lines) + b"\r\n\r\n" + data)
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()
