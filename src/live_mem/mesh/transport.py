"""Single-use, DNS-pinned HTTP transport for Project Mesh.

This module intentionally knows nothing about Mesh signatures or business
models.  It returns bounded raw control responses (or a bounded raw bootstrap
stream) so that the wire layer can authenticate bytes only after transport
limits and HTTP ambiguity checks have passed.
"""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import re
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

import httpx

from .destination import (
    DEFAULT_DNS_TIMEOUT_SECONDS,
    MeshDestination,
    MeshDestinationError,
    MeshResolver,
    ResolvedMeshDestination,
    resolve_destination,
    validate_mesh_path,
)

CONTROL_MAX_BYTES = 262_144
BOOTSTRAP_MAX_BYTES = 268_435_456
BOOTSTRAP_CHUNK_MAX_BYTES = 65_536

RESPONSE_HEADER_MAX_FIELDS = 32
RESPONSE_HEADER_MAX_WIRE_BYTES = 8_192
HEADER_NAME_MAX_BYTES = 64
HEADER_VALUE_MAX_BYTES = 4_096

REQUEST_HEADER_MAX_FIELDS = 64
REQUEST_HEADER_MAX_WIRE_BYTES = 16_384

DNS_TIMEOUT_SECONDS = 2.0
CONNECT_TIMEOUT_SECONDS = 3.0
WRITE_TIMEOUT_SECONDS = 5.0
CONTROL_IDLE_TIMEOUT_SECONDS = 10.0
CONTROL_TOTAL_TIMEOUT_SECONDS = 15.0
BOOTSTRAP_IDLE_TIMEOUT_SECONDS = 30.0
BOOTSTRAP_TOTAL_TIMEOUT_SECONDS = 300.0

_HEADER_NAME = re.compile(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_RESERVED_REQUEST_HEADERS = frozenset(
    {
        b"accept-encoding",
        b"authorization",
        b"connection",
        b"content-encoding",
        b"content-length",
        b"cookie",
        b"cookie2",
        b"host",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)


class MeshTransportError(RuntimeError):
    """Base class whose string never includes a hostname, IP, or payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MeshTransportUnavailable(MeshTransportError):
    """Untrusted edge/network/limit failure, never a protocol refusal."""


class MeshProtocolRefusal(MeshTransportError):
    """Optional wire-layer classification for an authenticated refusal."""


@dataclass(frozen=True, slots=True)
class MeshTransportLimits:
    control_max_bytes: int = CONTROL_MAX_BYTES
    bootstrap_max_bytes: int = BOOTSTRAP_MAX_BYTES

    def __post_init__(self) -> None:
        _bounded_int("control_max_bytes", self.control_max_bytes, CONTROL_MAX_BYTES)
        _bounded_int("bootstrap_max_bytes", self.bootstrap_max_bytes, BOOTSTRAP_MAX_BYTES)


@dataclass(frozen=True, slots=True)
class MeshTransportTimeouts:
    dns: float = DNS_TIMEOUT_SECONDS
    connect: float = CONNECT_TIMEOUT_SECONDS
    write: float = WRITE_TIMEOUT_SECONDS
    control_idle: float = CONTROL_IDLE_TIMEOUT_SECONDS
    control_total: float = CONTROL_TOTAL_TIMEOUT_SECONDS
    bootstrap_idle: float = BOOTSTRAP_IDLE_TIMEOUT_SECONDS
    bootstrap_total: float = BOOTSTRAP_TOTAL_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        maximums = {
            "dns": DNS_TIMEOUT_SECONDS,
            "connect": CONNECT_TIMEOUT_SECONDS,
            "write": WRITE_TIMEOUT_SECONDS,
            "control_idle": CONTROL_IDLE_TIMEOUT_SECONDS,
            "control_total": CONTROL_TOTAL_TIMEOUT_SECONDS,
            "bootstrap_idle": BOOTSTRAP_IDLE_TIMEOUT_SECONDS,
            "bootstrap_total": BOOTSTRAP_TOTAL_TIMEOUT_SECONDS,
        }
        for name, maximum in maximums.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
                or value > maximum
            ):
                raise ValueError(f"invalid_{name}_timeout")


@dataclass(frozen=True, slots=True)
class MeshTransportResponse:
    status_code: int
    headers: tuple[tuple[bytes, bytes], ...]
    body: bytes


class _PeerInfo(Protocol):
    def get_extra_info(self, info: str) -> Any: ...


TransportFactory = Callable[
    [ResolvedMeshDestination, ssl.SSLContext],
    httpx.AsyncBaseTransport,
]
ClientFactory = Callable[
    [httpx.AsyncBaseTransport, httpx.Timeout],
    httpx.AsyncClient,
]

_ValidatedT = TypeVar("_ValidatedT")
ResponseValidator = Callable[
    [MeshTransportResponse],
    _ValidatedT | Awaitable[_ValidatedT],
]


class MeshBootstrapStream:
    """Raw bounded response stream; it never parses or aggregates objects."""

    def __init__(
        self,
        response: httpx.Response,
        *,
        headers: tuple[tuple[bytes, bytes], ...],
        content_length: int | None,
        max_bytes: int,
    ) -> None:
        self.status_code = response.status_code
        self.headers = headers
        self._response = response
        self._content_length = content_length
        self._max_bytes = max_bytes
        self._bytes_read = 0
        self._closed = False
        self._iterator = response.aiter_raw(
            chunk_size=BOOTSTRAP_CHUNK_MAX_BYTES
        ).__aiter__()

    @property
    def bytes_read(self) -> int:
        return self._bytes_read

    def __aiter__(self) -> "MeshBootstrapStream":
        return self

    async def __anext__(self) -> bytes:
        if self._closed:
            raise StopAsyncIteration
        while True:
            try:
                chunk = await self._iterator.__anext__()
            except StopAsyncIteration:
                self._closed = True
                if (
                    self._content_length is not None
                    and self._bytes_read != self._content_length
                ):
                    await self._response.aclose()
                    raise MeshTransportUnavailable(
                        "mesh_response_length_mismatch"
                    )
                raise
            except httpx.HTTPError:
                self._closed = True
                await self._response.aclose()
                raise MeshTransportUnavailable("mesh_transport_unavailable") from None
            if chunk:
                break
        if len(chunk) > BOOTSTRAP_CHUNK_MAX_BYTES:
            self._closed = True
            await self._response.aclose()
            raise MeshTransportUnavailable("mesh_response_limit_exceeded")
        if self._bytes_read + len(chunk) > self._max_bytes:
            # Validate the entire next chunk before yielding any part of it.
            self._closed = True
            await self._response.aclose()
            raise MeshTransportUnavailable("mesh_response_limit_exceeded")
        self._bytes_read += len(chunk)
        return chunk

    async def aclose(self) -> None:
        self._closed = True
        await self._response.aclose()


class HttpPeerTransport(Generic[_ValidatedT]):
    """One DNS resolution, socket, HTTP transport, and client per request."""

    def __init__(
        self,
        destination: MeshDestination | str,
        *,
        resolver: MeshResolver | None = None,
        limits: MeshTransportLimits | None = None,
        timeouts: MeshTransportTimeouts | None = None,
        ssl_context: ssl.SSLContext | None = None,
        test_allow_http_loopback: bool = False,
        transport_factory: TransportFactory | None = None,
        client_factory: ClientFactory | None = None,
    ) -> None:
        if type(test_allow_http_loopback) is not bool:
            raise MeshDestinationError()
        if type(destination) is MeshDestination:
            self.destination = destination.revalidated(
                test_allow_http_loopback=test_allow_http_loopback
            )
        elif type(destination) is str:
            self.destination = MeshDestination.parse(
                destination,
                test_allow_http_loopback=test_allow_http_loopback,
            )
        else:
            raise MeshDestinationError()
        self._test_allow_http_loopback = test_allow_http_loopback
        self._resolver = resolver
        self.limits = limits or MeshTransportLimits()
        self.timeouts = timeouts or MeshTransportTimeouts()
        self._ssl_context = ssl_context or _secure_ssl_context()
        _validate_ssl_context(self._ssl_context)
        self._transport_factory = transport_factory
        self._client_factory = client_factory

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: Sequence[tuple[str | bytes, str | bytes]] = (),
        response_validator: ResponseValidator[_ValidatedT] | None = None,
    ) -> MeshTransportResponse | _ValidatedT:
        """Collect one control response after raw byte/header validation."""

        method, path, body, request_headers = self._prepare_request(
            method,
            path,
            body,
            headers,
        )
        if len(body) > self.limits.control_max_bytes:
            raise MeshTransportUnavailable("mesh_request_limit_exceeded")

        try:
            async with asyncio.timeout(self.timeouts.control_total):
                async with self._open_response(
                    method,
                    path,
                    body=body,
                    headers=request_headers,
                    idle_timeout=self.timeouts.control_idle,
                    max_response_bytes=self.limits.control_max_bytes,
                ) as (response, response_headers, content_length):
                    collected = bytearray()
                    async for chunk in response.aiter_raw():
                        if len(collected) + len(chunk) > self.limits.control_max_bytes:
                            await response.aclose()
                            raise MeshTransportUnavailable(
                                "mesh_response_limit_exceeded"
                            )
                        collected.extend(chunk)
                    if (
                        content_length is not None
                        and len(collected) != content_length
                    ):
                        raise MeshTransportUnavailable(
                            "mesh_response_length_mismatch"
                        )
                    bounded = MeshTransportResponse(
                        status_code=response.status_code,
                        headers=response_headers,
                        body=bytes(collected),
                    )
                    if response_validator is None:
                        return bounded
                    validated = response_validator(bounded)
                    if inspect.isawaitable(validated):
                        return await validated
                    return validated
        except MeshTransportUnavailable:
            raise
        except MeshDestinationError:
            raise MeshTransportUnavailable("mesh_transport_unavailable") from None
        except (TimeoutError, httpx.TimeoutException):
            raise MeshTransportUnavailable("mesh_transport_timeout") from None
        except (httpx.HTTPError, OSError, ssl.SSLError):
            raise MeshTransportUnavailable("mesh_transport_unavailable") from None

    @asynccontextmanager
    async def stream_bootstrap(
        self,
        method: str,
        path: str,
        *,
        body: bytes = b"",
        headers: Sequence[tuple[str | bytes, str | bytes]] = (),
    ) -> AsyncIterator[MeshBootstrapStream]:
        """Open one bounded bootstrap stream without buffering or parsing it."""

        method, path, body, request_headers = self._prepare_request(
            method,
            path,
            body,
            headers,
        )
        if len(body) > self.limits.control_max_bytes:
            raise MeshTransportUnavailable("mesh_request_limit_exceeded")

        try:
            async with asyncio.timeout(self.timeouts.bootstrap_total):
                async with self._open_response(
                    method,
                    path,
                    body=body,
                    headers=request_headers,
                    idle_timeout=self.timeouts.bootstrap_idle,
                    max_response_bytes=self.limits.bootstrap_max_bytes,
                ) as (response, response_headers, content_length):
                    stream = MeshBootstrapStream(
                        response,
                        headers=response_headers,
                        content_length=content_length,
                        max_bytes=self.limits.bootstrap_max_bytes,
                    )
                    try:
                        yield stream
                    finally:
                        await stream.aclose()
        except MeshTransportUnavailable:
            raise
        except MeshDestinationError:
            raise MeshTransportUnavailable("mesh_transport_unavailable") from None
        except (TimeoutError, httpx.TimeoutException):
            raise MeshTransportUnavailable("mesh_transport_timeout") from None
        except (httpx.HTTPError, OSError, ssl.SSLError):
            raise MeshTransportUnavailable("mesh_transport_unavailable") from None

    def _prepare_request(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: Sequence[tuple[str | bytes, str | bytes]],
    ) -> tuple[str, str, bytes, tuple[tuple[bytes, bytes], ...]]:
        if method not in {"GET", "POST"}:
            raise MeshTransportUnavailable("mesh_invalid_request")
        path = validate_mesh_path(path)
        if not isinstance(body, bytes):
            raise MeshTransportUnavailable("mesh_invalid_request")
        if method == "GET" and body:
            raise MeshTransportUnavailable("mesh_invalid_request")

        caller_headers = _coerce_request_headers(headers)
        generated = (
            (b"host", self.destination.authority.encode("ascii")),
            (b"accept-encoding", b"identity"),
            (b"connection", b"close"),
        )
        combined = generated + caller_headers
        _validate_header_block(
            combined,
            max_fields=REQUEST_HEADER_MAX_FIELDS,
            max_wire_bytes=REQUEST_HEADER_MAX_WIRE_BYTES,
        )
        return method, path, body, combined

    @asynccontextmanager
    async def _open_response(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: tuple[tuple[bytes, bytes], ...],
        idle_timeout: float,
        max_response_bytes: int,
    ) -> AsyncIterator[
        tuple[
            httpx.Response,
            tuple[tuple[bytes, bytes], ...],
            int | None,
        ]
    ]:
        resolved = await resolve_destination(
            self.destination,
            resolver=self._resolver,
            timeout_seconds=self.timeouts.dns,
            test_allow_http_loopback=self._test_allow_http_loopback,
        )
        transport = self._make_transport(resolved)
        timeout = httpx.Timeout(
            connect=self.timeouts.connect,
            read=idle_timeout,
            write=self.timeouts.write,
            pool=self.timeouts.connect,
        )
        client = self._make_client(transport, timeout)
        response: httpx.Response | None = None
        async with client:
            request = client.build_request(
                method,
                resolved.numeric_url(path),
                headers=headers,
                content=body,
            )
            request.extensions["sni_hostname"] = resolved.tls_server_name
            _validate_header_block(
                tuple(request.headers.raw),
                max_fields=REQUEST_HEADER_MAX_FIELDS,
                max_wire_bytes=REQUEST_HEADER_MAX_WIRE_BYTES,
            )
            try:
                response = await client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
                _validate_http_version(response)
                _validate_peer_address(response, resolved)
                response_headers, content_length = _validate_response_headers(
                    response,
                    max_body_bytes=max_response_bytes,
                )
                if 300 <= response.status_code <= 399:
                    raise MeshTransportUnavailable("mesh_redirect_refused")
                if response.status_code == 413:
                    # An edge-generated 413 is unsigned and can never be treated
                    # as an authenticated protocol response.
                    raise MeshTransportUnavailable("mesh_edge_response_untrusted")
                yield response, response_headers, content_length
            finally:
                if response is not None:
                    await response.aclose()

    def _make_transport(
        self,
        resolved: ResolvedMeshDestination,
    ) -> httpx.AsyncBaseTransport:
        if self._transport_factory is not None:
            return self._transport_factory(resolved, self._ssl_context)
        return httpx.AsyncHTTPTransport(
            verify=self._ssl_context,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            retries=0,
            http1=True,
            http2=False,
        )

    def _make_client(
        self,
        transport: httpx.AsyncBaseTransport,
        timeout: httpx.Timeout,
    ) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory(transport, timeout)
        return httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            max_redirects=0,
            trust_env=False,
            http1=True,
            http2=False,
        )


def _secure_ssl_context() -> ssl.SSLContext:
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _validate_ssl_context(context: ssl.SSLContext) -> None:
    if (
        not context.check_hostname
        or context.verify_mode != ssl.CERT_REQUIRED
        or context.minimum_version < ssl.TLSVersion.TLSv1_2
    ):
        raise ValueError("unsafe_mesh_tls_context")


def _bounded_int(name: str, value: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"invalid_{name}")


def _as_header_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise MeshTransportUnavailable("mesh_invalid_headers")
    try:
        return value.encode("ascii")
    except UnicodeEncodeError:
        raise MeshTransportUnavailable("mesh_invalid_headers") from None


def _coerce_request_headers(
    headers: Sequence[tuple[str | bytes, str | bytes]],
) -> tuple[tuple[bytes, bytes], ...]:
    if isinstance(headers, (str, bytes)):
        raise MeshTransportUnavailable("mesh_invalid_headers")
    result: list[tuple[bytes, bytes]] = []
    try:
        iterator = iter(headers)
    except TypeError:
        raise MeshTransportUnavailable("mesh_invalid_headers") from None
    for item in iterator:
        if not isinstance(item, tuple) or len(item) != 2:
            raise MeshTransportUnavailable("mesh_invalid_headers")
        name = _as_header_bytes(item[0])
        value = _as_header_bytes(item[1])
        if name.lower() in _RESERVED_REQUEST_HEADERS:
            raise MeshTransportUnavailable("mesh_invalid_headers")
        result.append((name, value))
    return tuple(result)


def _validate_header_block(
    headers: tuple[tuple[bytes, bytes], ...],
    *,
    max_fields: int,
    max_wire_bytes: int,
) -> dict[bytes, bytes]:
    if len(headers) > max_fields:
        raise MeshTransportUnavailable("mesh_header_limit_exceeded")
    seen: dict[bytes, bytes] = {}
    wire_bytes = 0
    for name, value in headers:
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            raise MeshTransportUnavailable("mesh_invalid_headers")
        if (
            not name
            or len(name) > HEADER_NAME_MAX_BYTES
            or len(value) > HEADER_VALUE_MAX_BYTES
            or not _HEADER_NAME.fullmatch(name)
            or any(byte > 0x7F for byte in value)
            or b"\r" in value
            or b"\n" in value
            or b"\x00" in value
        ):
            raise MeshTransportUnavailable("mesh_invalid_headers")
        lowered = name.lower()
        if lowered in seen:
            raise MeshTransportUnavailable("mesh_ambiguous_headers")
        seen[lowered] = value
        wire_bytes += len(name) + 2 + len(value) + 2
        if wire_bytes > max_wire_bytes:
            raise MeshTransportUnavailable("mesh_header_limit_exceeded")
    return seen


def _validate_response_headers(
    response: httpx.Response,
    *,
    max_body_bytes: int,
) -> tuple[tuple[tuple[bytes, bytes], ...], int | None]:
    raw = tuple(response.headers.raw)
    by_name = _validate_header_block(
        raw,
        max_fields=RESPONSE_HEADER_MAX_FIELDS,
        max_wire_bytes=RESPONSE_HEADER_MAX_WIRE_BYTES,
    )
    if b"content-encoding" in by_name or b"trailer" in by_name:
        raise MeshTransportUnavailable("mesh_response_encoding_refused")

    content_length: int | None = None
    if b"content-length" in by_name:
        value = by_name[b"content-length"]
        if (
            not value
            or len(value) > 20
            or not value.isdigit()
            or (len(value) > 1 and value.startswith(b"0"))
        ):
            raise MeshTransportUnavailable("mesh_ambiguous_headers")
        content_length = int(value)
        if content_length > max_body_bytes:
            raise MeshTransportUnavailable("mesh_response_limit_exceeded")

    if b"transfer-encoding" in by_name:
        if by_name[b"transfer-encoding"] != b"chunked":
            raise MeshTransportUnavailable("mesh_ambiguous_headers")
        if content_length is not None:
            raise MeshTransportUnavailable("mesh_ambiguous_headers")
    return raw, content_length


def _validate_http_version(response: httpx.Response) -> None:
    if response.extensions.get("http_version") != b"HTTP/1.1":
        raise MeshTransportUnavailable("mesh_http_version_refused")


def _validate_peer_address(
    response: httpx.Response,
    resolved: ResolvedMeshDestination,
) -> None:
    stream = response.extensions.get("network_stream")
    if stream is None or not hasattr(stream, "get_extra_info"):
        raise MeshTransportUnavailable("mesh_peer_address_unavailable")
    try:
        server_address = stream.get_extra_info("server_addr")
        if not isinstance(server_address, tuple) or len(server_address) < 2:
            raise ValueError("missing peer socket tuple")
        raw = server_address[0]
        port = server_address[1]
        peer = ipaddress.ip_address(raw)
    except (TypeError, ValueError, IndexError):
        raise MeshTransportUnavailable("mesh_peer_address_unavailable") from None
    if (
        peer != resolved.selected_ip
        or isinstance(port, bool)
        or not isinstance(port, int)
        or port != resolved.destination.port
    ):
        raise MeshTransportUnavailable("mesh_peer_address_mismatch")


__all__ = [
    "BOOTSTRAP_CHUNK_MAX_BYTES",
    "BOOTSTRAP_IDLE_TIMEOUT_SECONDS",
    "BOOTSTRAP_MAX_BYTES",
    "BOOTSTRAP_TOTAL_TIMEOUT_SECONDS",
    "CONNECT_TIMEOUT_SECONDS",
    "CONTROL_IDLE_TIMEOUT_SECONDS",
    "CONTROL_MAX_BYTES",
    "CONTROL_TOTAL_TIMEOUT_SECONDS",
    "DNS_TIMEOUT_SECONDS",
    "HEADER_NAME_MAX_BYTES",
    "HEADER_VALUE_MAX_BYTES",
    "HttpPeerTransport",
    "MeshBootstrapStream",
    "MeshProtocolRefusal",
    "MeshTransportError",
    "MeshTransportLimits",
    "MeshTransportResponse",
    "MeshTransportTimeouts",
    "MeshTransportUnavailable",
    "RESPONSE_HEADER_MAX_FIELDS",
    "RESPONSE_HEADER_MAX_WIRE_BYTES",
    "WRITE_TIMEOUT_SECONDS",
]
