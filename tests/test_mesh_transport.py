"""Mutation-oriented tests for the Project Mesh destination/transport boundary."""

from __future__ import annotations

import asyncio
import inspect
import ipaddress
import ssl
import sys
import traceback
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from live_mem.mesh import transport as transport_module
from live_mem.mesh.destination import (
    IANA_IPV4_SPECIAL_PURPOSE_CIDRS,
    IANA_IPV6_SPECIAL_PURPOSE_CIDRS,
    IANA_SPECIAL_REGISTRY_LAST_UPDATED,
    IANA_SPECIAL_REGISTRY_VERIFIED,
    MeshDestination,
    MeshDestinationError,
    ResolvedMeshDestination,
    is_public_mesh_address,
    resolve_destination,
    validate_mesh_path,
)
from live_mem.mesh.transport import (
    BOOTSTRAP_CHUNK_MAX_BYTES,
    HttpPeerTransport,
    MeshTransportLimits,
    MeshTransportTimeouts,
    MeshTransportUnavailable,
)

PUBLIC_IPV4 = "93.184.216.34"
SECOND_PUBLIC_IPV4 = "8.8.8.8"

EXPECTED_IPV4_IANA = (
    "0.0.0.0/8",
    "0.0.0.0/32",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.0.0/29",
    "192.0.0.8/32",
    "192.0.0.9/32",
    "192.0.0.10/32",
    "192.0.0.170/32",
    "192.0.0.171/32",
    "192.0.2.0/24",
    "192.31.196.0/24",
    "192.52.193.0/24",
    "192.88.99.0/24",
    "192.88.99.2/32",
    "192.168.0.0/16",
    "192.175.48.0/24",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "240.0.0.0/4",
    "255.255.255.255/32",
)
EXPECTED_IPV6_IANA = (
    "::1/128",
    "::/128",
    "::ffff:0:0/96",
    "64:ff9b::/96",
    "64:ff9b:1::/48",
    "100::/64",
    "100:0:0:1::/64",
    "2001::/23",
    "2001::/32",
    "2001:1::1/128",
    "2001:1::2/128",
    "2001:1::3/128",
    "2001:2::/48",
    "2001:3::/32",
    "2001:4:112::/48",
    "2001:10::/28",
    "2001:20::/28",
    "2001:30::/28",
    "2001:db8::/32",
    "2002::/16",
    "2620:4f:8000::/48",
    "3fff::/20",
    "5f00::/16",
    "fc00::/7",
    "fe80::/10",
)


class FakeResolver:
    def __init__(
        self,
        *answers: Sequence[str] | BaseException,
        delay: float = 0,
    ) -> None:
        self.answers = answers
        self.delay = delay
        self.calls: list[tuple[str, int]] = []

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        self.calls.append((host, port))
        if self.delay:
            await asyncio.sleep(self.delay)
        index = min(len(self.calls) - 1, len(self.answers) - 1)
        answer = self.answers[index]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakePeerStream:
    def __init__(self, address: str, port: int = 443) -> None:
        self.address = address
        self.port = port

    def get_extra_info(self, info: str) -> Any:
        if info == "server_addr":
            return (self.address, self.port)
        return None


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: Sequence[bytes], *, delay: float = 0) -> None:
        self.chunks = tuple(chunks)
        self.delay = delay
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def mesh_response(
    *,
    status: int = 503,
    peer: str = PUBLIC_IPV4,
    peer_port: int = 443,
    headers: Sequence[tuple[bytes, bytes]] = (),
    content: bytes | None = b"{}",
    stream: httpx.AsyncByteStream | None = None,
) -> httpx.Response:
    extensions = {
        "http_version": b"HTTP/1.1",
        "network_stream": FakePeerStream(peer, peer_port),
    }
    if stream is not None:
        return httpx.Response(
            status,
            headers=headers,
            stream=stream,
            extensions=extensions,
        )
    return httpx.Response(
        status,
        headers=headers,
        stream=ChunkStream((b"" if content is None else content,)),
        extensions=extensions,
    )


Handler = Callable[
    [httpx.Request, Any],
    httpx.Response | Awaitable[httpx.Response],
]


class RecordingTransportFactory:
    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.resolved: list[Any] = []
        self.requests: list[httpx.Request] = []
        self.transports: list[httpx.MockTransport] = []

    def __call__(self, resolved, ssl_context: ssl.SSLContext):
        assert ssl_context.check_hostname
        self.resolved.append(resolved)

        async def dispatch(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            result = self.handler(request, resolved)
            if inspect.isawaitable(result):
                return await result
            return result

        transport = httpx.MockTransport(dispatch)
        self.transports.append(transport)
        return transport


def make_transport(
    factory: RecordingTransportFactory,
    *,
    destination: str = "https://peer.example",
    resolver: FakeResolver | None = None,
    limits: MeshTransportLimits | None = None,
    timeouts: MeshTransportTimeouts | None = None,
) -> HttpPeerTransport:
    return HttpPeerTransport(
        destination,
        resolver=resolver or FakeResolver((PUBLIC_IPV4,)),
        limits=limits,
        timeouts=timeouts,
        transport_factory=factory,
    )


def _tls_contexts(
    tmp_path: Path,
    *,
    certificate_hostname: str,
) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mesh test CA")])
    ca_subject_key_identifier = x509.SubjectKeyIdentifier.from_public_key(
        ca_key.public_key()
    )
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(ca_subject_key_identifier, critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, certificate_hostname)]
    )
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(certificate_hostname)]),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier(
                key_identifier=ca_subject_key_identifier.digest,
                authority_cert_issuer=None,
                authority_cert_serial_number=None,
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    certificate_path = tmp_path / f"{certificate_hostname}.certificate.pem"
    key_path = tmp_path / f"{certificate_hostname}.key.pem"
    certificate_path.write_bytes(server_certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(certificate_path, key_path)
    client_context = ssl.create_default_context(
        cadata=ca_certificate.public_bytes(serialization.Encoding.PEM).decode("ascii")
    )
    client_context.minimum_version = ssl.TLSVersion.TLSv1_2
    return server_context, client_context


def special_samples(cidr: str) -> tuple[str, str, str]:
    network = ipaddress.ip_network(cidr)
    interior_offset = network.num_addresses // 2
    interior = ipaddress.ip_address(int(network.network_address) + interior_offset)
    return (
        network.network_address.compressed,
        interior.compressed,
        network.broadcast_address.compressed,
    )


IANA_CASES = tuple(
    (cidr, sample)
    for cidr in EXPECTED_IPV4_IANA + EXPECTED_IPV6_IANA
    for sample in special_samples(cidr)
)


def test_iana_snapshot_is_exact_and_mutation_sensitive() -> None:
    assert sys.version_info >= (3, 11)
    assert IANA_SPECIAL_REGISTRY_LAST_UPDATED == "2025-10-09"
    assert IANA_SPECIAL_REGISTRY_VERIFIED == "2026-07-15"
    assert IANA_IPV4_SPECIAL_PURPOSE_CIDRS == EXPECTED_IPV4_IANA
    assert IANA_IPV6_SPECIAL_PURPOSE_CIDRS == EXPECTED_IPV6_IANA
    assert "100:0:0:1::/64" in IANA_IPV6_SPECIAL_PURPOSE_CIDRS
    assert "5f00::/16" in IANA_IPV6_SPECIAL_PURPOSE_CIDRS


@pytest.mark.parametrize(("cidr", "sample"), IANA_CASES)
def test_every_iana_network_boundary_and_interior_is_denied(
    cidr: str,
    sample: str,
) -> None:
    assert not is_public_mesh_address(sample), (cidr, sample)


@pytest.mark.asyncio
@pytest.mark.parametrize(("cidr", "sample"), IANA_CASES)
async def test_every_iana_literal_and_dns_answer_is_denied(
    cidr: str,
    sample: str,
) -> None:
    address = ipaddress.ip_address(sample)
    literal = f"https://[{sample}]" if address.version == 6 else f"https://{sample}"
    with pytest.raises(MeshDestinationError):
        await resolve_destination(MeshDestination.parse(literal))

    destination = MeshDestination.parse("https://peer.example")
    with pytest.raises(MeshDestinationError):
        await resolve_destination(destination, resolver=FakeResolver((sample,)))


@pytest.mark.parametrize(
    "address",
    (
        "::192.0.2.1",
        "::ffff:8.8.8.8",
        "64:ff9b::808:808",
        "64:ff9b:1::808:808",
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
        "2002:0808:0808::",
        "fec0::1",
        "ff02::1",
        "100.100.100.200",
        "168.63.129.16",
        "169.254.169.254",
    ),
)
def test_transition_metadata_and_special_forms_are_denied(address: str) -> None:
    assert not is_public_mesh_address(address)


@pytest.mark.parametrize(
    "address",
    ("8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"),
)
def test_regular_global_unicast_is_allowed(address: str) -> None:
    assert is_public_mesh_address(address)


@pytest.mark.parametrize(
    "url",
    (
        "",
        "HTTP://127.0.0.1",
        "http://peer.example",
        "http://localhost",
        "https://user@peer.example",
        "https://@peer.example",
        "https://user%40peer.example",
        "https://peer.example?x=1",
        "https://peer.example?",
        "https://peer.example#x",
        "https://peer.example/path",
        "https://peer.example/%2e",
        "https://peer.example\\evil",
        "https://péér.example",
        "https://PEER.example",
        "https://peer.example.",
        "https://peer_name.example",
        "https://localhost",
        "https://010.000.000.001",
        "https://2130706433",
        "https://peer.example:0",
        "https://peer.example:65536",
        " https://peer.example",
    ),
)
def test_strict_public_url_rejects_ambiguous_forms(url: str) -> None:
    with pytest.raises(MeshDestinationError):
        MeshDestination.parse(url)


def test_destination_canonicalizes_root_and_default_port() -> None:
    implicit = MeshDestination.parse("https://peer.example/")
    explicit = MeshDestination.parse("https://peer.example:443")
    custom = MeshDestination.parse("https://peer.example:8443")
    assert implicit.canonical_url == "https://peer.example"
    assert explicit.canonical_url == implicit.canonical_url
    assert custom.authority == "peer.example:8443"


def test_http_test_seam_is_literal_loopback_only() -> None:
    destination = MeshDestination.parse(
        "http://127.0.0.1:8080",
        test_allow_http_loopback=True,
    )
    assert destination.test_loopback_http
    with pytest.raises(MeshDestinationError):
        MeshDestination.parse(
            "http://localhost:8080",
            test_allow_http_loopback=True,
        )
    with pytest.raises(MeshDestinationError):
        MeshDestination.parse(
            "http://8.8.8.8",
            test_allow_http_loopback=True,
        )


@pytest.mark.asyncio
async def test_preconstructed_destination_cannot_bypass_parser_or_ssrf_policy() -> None:
    forged = MeshDestination(
        scheme="http",
        host="8.8.8.8",
        port=80,
        authority="8.8.8.8",
        canonical_url="http://8.8.8.8",
        literal_ip=ipaddress.ip_address("8.8.8.8"),
    )
    with pytest.raises(MeshDestinationError):
        HttpPeerTransport(forged)
    with pytest.raises(MeshDestinationError) as caught:
        await resolve_destination(forged)
    assert str(caught.value) == "mesh_resolution_failed"


@pytest.mark.asyncio
async def test_preconstructed_destination_fields_must_exactly_match_canonical_url() -> None:
    forged = MeshDestination(
        scheme="https",
        host="peer.example",
        port=True,
        authority="peer.example",
        canonical_url="https://peer.example",
        literal_ip=ipaddress.ip_address("127.0.0.1"),
    )
    with pytest.raises(MeshDestinationError):
        HttpPeerTransport(forged)
    with pytest.raises(MeshDestinationError):
        await resolve_destination(forged)


@pytest.mark.asyncio
async def test_preparsed_http_loopback_still_requires_explicit_consumer_test_seam() -> None:
    destination = MeshDestination.parse(
        "http://127.0.0.1:8080",
        test_allow_http_loopback=True,
    )
    with pytest.raises(MeshDestinationError):
        HttpPeerTransport(destination)
    with pytest.raises(MeshDestinationError):
        await resolve_destination(destination)

    transport = HttpPeerTransport(
        destination,
        test_allow_http_loopback=True,
    )
    assert transport.destination == destination
    resolved = await resolve_destination(
        destination,
        test_allow_http_loopback=True,
    )
    assert resolved.selected_ip == ipaddress.ip_address("127.0.0.1")


@pytest.mark.parametrize("test_seam", (1, None, "yes"))
def test_http_loopback_test_seam_requires_an_exact_boolean(test_seam: object) -> None:
    with pytest.raises(MeshDestinationError):
        MeshDestination.parse(
            "http://127.0.0.1",
            test_allow_http_loopback=test_seam,  # type: ignore[arg-type]
        )
    with pytest.raises(MeshDestinationError):
        HttpPeerTransport(
            "http://127.0.0.1",
            test_allow_http_loopback=test_seam,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "path",
    (
        "/mesh/v1//events",
        "/mesh/v1/../events",
        "/mesh/v1/./events",
        "/mesh/v1/events/",
        "/mesh/v1/%2fevents",
        "/mesh/v1/%2Fevents",
        "/mesh/v1/pair\\id/status",
        "/mesh/v1/events?x=1",
        "/api/mesh/v1/events",
    ),
)
def test_mesh_path_rejects_alias_and_fallthrough_forms(path: str) -> None:
    with pytest.raises(MeshDestinationError):
        validate_mesh_path(path)


def test_mesh_path_accepts_canonical_route_matrix_examples() -> None:
    assert validate_mesh_path("/mesh/v1/pair/claim")
    assert validate_mesh_path("/mesh/v1/pair/pair-01/status")
    assert validate_mesh_path("/mesh/v1/pair/pair-01/bootstrap")
    assert validate_mesh_path("/mesh/v1/pair/pair-01/ack")
    assert validate_mesh_path("/mesh/v1/events")


@pytest.mark.asyncio
async def test_mixed_dns_answers_fail_before_any_socket() -> None:
    resolver = FakeResolver(((PUBLIC_IPV4, "127.0.0.1")))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(factory, resolver=resolver)
    with pytest.raises(MeshTransportUnavailable):
        await client.request("POST", "/mesh/v1/events")
    assert factory.requests == []


@pytest.mark.asyncio
async def test_more_than_sixteen_dns_answers_fails_closed() -> None:
    resolver = FakeResolver((tuple(f"8.8.8.{value}" for value in range(1, 18))))
    destination = MeshDestination.parse("https://peer.example")
    with pytest.raises(MeshDestinationError):
        await resolve_destination(destination, resolver=resolver)


@pytest.mark.asyncio
async def test_dns_rebinding_is_re_resolved_and_second_request_is_blocked() -> None:
    resolver = FakeResolver((PUBLIC_IPV4,), ("169.254.169.254",))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(factory, resolver=resolver)
    await client.request("POST", "/mesh/v1/events")
    with pytest.raises(MeshTransportUnavailable):
        await client.request("POST", "/mesh/v1/events")
    assert len(resolver.calls) == 2
    assert len(factory.requests) == 1


@pytest.mark.asyncio
async def test_numeric_pin_preserves_host_and_sni_and_checks_peer_socket() -> None:
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(factory)
    await client.request("GET", "/mesh/v1/pair/pair-01/status")
    request = factory.requests[0]
    assert request.url.host == PUBLIC_IPV4
    assert request.headers["host"] == "peer.example"
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["connection"] == "close"
    assert request.extensions["sni_hostname"] == "peer.example"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "certificate_hostname, succeeds",
    [("peer.test", True), ("wrong.test", False)],
)
async def test_real_tls_handshake_pins_numeric_socket_but_verifies_original_hostname(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    certificate_hostname: str,
    succeeds: bool,
) -> None:
    server_context, client_context = _tls_contexts(
        tmp_path,
        certificate_hostname=certificate_hostname,
    )
    observed_sni: list[str | None] = []
    observed_request: list[bytes] = []

    def record_sni(
        _socket: ssl.SSLSocket,
        server_name: str | None,
        _context: ssl.SSLContext,
    ) -> None:
        observed_sni.append(server_name)

    server_context.set_servername_callback(record_sni)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            observed_request.append(head)
            content_length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    content_length = int(line.split(b":", 1)[1].strip())
            if content_length:
                await reader.readexactly(content_length)
            writer.write(
                b"HTTP/1.1 503 Service Unavailable\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: 2\r\n"
                b"Connection: close\r\n\r\n{}"
            )
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, ssl.SSLError):
                pass

    server = await asyncio.start_server(
        handle,
        "127.0.0.1",
        0,
        ssl=server_context,
    )
    socket_info = server.sockets[0].getsockname()
    port = socket_info[1]
    destination = MeshDestination.parse(f"https://peer.test:{port}")
    pinned = ResolvedMeshDestination(
        destination=destination,
        addresses=(ipaddress.ip_address("127.0.0.1"),),
        selected_ip=ipaddress.ip_address("127.0.0.1"),
    )

    async def resolve_pinned(*_args: Any, **_kwargs: Any) -> ResolvedMeshDestination:
        return pinned

    monkeypatch.setattr(transport_module, "resolve_destination", resolve_pinned)
    client = HttpPeerTransport(destination, ssl_context=client_context)
    try:
        if succeeds:
            response = await client.request("POST", "/mesh/v1/events", body=b"{}")
            assert response.status_code == 503
            assert response.body == b"{}"
            assert len(observed_request) == 1
            assert f"host: peer.test:{port}\r\n".encode("ascii") in observed_request[0].lower()
        else:
            with pytest.raises(MeshTransportUnavailable) as raised:
                await client.request("POST", "/mesh/v1/events", body=b"{}")
            assert raised.value.code == "mesh_transport_unavailable"
            assert "peer.test" not in str(raised.value)
            assert observed_request == []
        # The request URL itself is numeric.  Success with a DNS-only
        # certificate therefore proves that httpcore consumed the explicit SNI
        # extension and performed hostname verification against the origin.
        assert observed_sni == ["peer.test"]
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_peer_socket_mismatch_is_refused() -> None:
    factory = RecordingTransportFactory(
        lambda _request, _resolved: mesh_response(peer=SECOND_PUBLIC_IPV4)
    )
    client = make_transport(factory)
    with pytest.raises(MeshTransportUnavailable, match="mesh_peer_address_mismatch"):
        await client.request("POST", "/mesh/v1/events")


@pytest.mark.asyncio
async def test_peer_socket_port_mismatch_is_refused() -> None:
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            peer=str(resolved.selected_ip),
            peer_port=8443,
        )
    )
    client = make_transport(factory)
    with pytest.raises(MeshTransportUnavailable, match="mesh_peer_address_mismatch"):
        await client.request("POST", "/mesh/v1/events")


@pytest.mark.asyncio
async def test_every_request_gets_a_new_client_transport() -> None:
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(factory)
    await client.request("POST", "/mesh/v1/events")
    await client.request("POST", "/mesh/v1/events")
    assert len(factory.transports) == 2
    assert factory.transports[0] is not factory.transports[1]


@pytest.mark.asyncio
async def test_cross_host_same_ip_never_reuses_connection_or_sni() -> None:
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    first = make_transport(factory, destination="https://one.example")
    second = make_transport(factory, destination="https://two.example")
    await first.request("POST", "/mesh/v1/events")
    await second.request("POST", "/mesh/v1/events")
    assert len(factory.transports) == 2
    assert [request.headers["host"] for request in factory.requests] == [
        "one.example",
        "two.example",
    ]
    assert [request.extensions["sni_hostname"] for request in factory.requests] == [
        "one.example",
        "two.example",
    ]


@pytest.mark.asyncio
async def test_environment_proxy_is_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    captured: list[dict[str, Any]] = []
    real_client = transport_module.httpx.AsyncClient

    def recording_client(*args, **kwargs):
        captured.append(kwargs.copy())
        return real_client(*args, **kwargs)

    monkeypatch.setattr(transport_module.httpx, "AsyncClient", recording_client)
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(factory)
    await client.request("POST", "/mesh/v1/events")
    assert captured[0]["trust_env"] is False
    assert captured[0]["follow_redirects"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("status", (301, 302, 303, 307, 308))
async def test_redirects_are_never_followed_or_validated(status: int) -> None:
    validator_calls: list[object] = []
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            status=status,
            peer=str(resolved.selected_ip),
            headers=((b"location", b"https://evil.example/mesh/v1/events"),),
        )
    )
    client = make_transport(factory)
    with pytest.raises(MeshTransportUnavailable, match="mesh_redirect_refused"):
        await client.request(
            "POST",
            "/mesh/v1/events",
            response_validator=lambda response: validator_calls.append(response),
        )
    assert len(factory.requests) == 1
    assert validator_calls == []


@pytest.mark.asyncio
async def test_edge_413_is_untrusted_and_never_reaches_wire_validator() -> None:
    validator_calls: list[object] = []
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            status=413,
            peer=str(resolved.selected_ip),
        )
    )
    client = make_transport(factory)
    with pytest.raises(MeshTransportUnavailable, match="mesh_edge_response_untrusted"):
        await client.request(
            "POST",
            "/mesh/v1/events",
            response_validator=lambda response: validator_calls.append(response),
        )
    assert validator_calls == []


def test_transport_limits_are_only_resserrable() -> None:
    MeshTransportLimits(control_max_bytes=1, bootstrap_max_bytes=1)
    with pytest.raises(ValueError):
        MeshTransportLimits(control_max_bytes=262_145)
    with pytest.raises(ValueError):
        MeshTransportLimits(bootstrap_max_bytes=268_435_457)
    with pytest.raises(ValueError):
        MeshTransportLimits(control_max_bytes=True)


def test_transport_timeouts_are_only_resserrable() -> None:
    MeshTransportTimeouts(dns=0.1, control_total=1, bootstrap_total=2)
    with pytest.raises(ValueError):
        MeshTransportTimeouts(dns=2.1)
    with pytest.raises(ValueError):
        MeshTransportTimeouts(connect=3.1)
    with pytest.raises(ValueError):
        MeshTransportTimeouts(control_total=15.1)
    with pytest.raises(ValueError):
        MeshTransportTimeouts(bootstrap_total=300.1)


@pytest.mark.asyncio
async def test_control_request_limit_rejects_before_dns_or_socket() -> None:
    resolver = FakeResolver((PUBLIC_IPV4,))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(
        factory,
        resolver=resolver,
        limits=MeshTransportLimits(control_max_bytes=4),
    )
    with pytest.raises(MeshTransportUnavailable, match="mesh_request_limit_exceeded"):
        await client.request("POST", "/mesh/v1/events", body=b"12345")
    assert resolver.calls == []
    assert factory.requests == []


@pytest.mark.asyncio
async def test_control_response_exact_limit_is_collected_after_raw_cap() -> None:
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            peer=str(resolved.selected_ip),
            content=b"1234",
        )
    )
    client = make_transport(
        factory,
        limits=MeshTransportLimits(control_max_bytes=4),
    )
    result = await client.request("POST", "/mesh/v1/events")
    assert result.body == b"1234"


@pytest.mark.asyncio
async def test_control_stream_plus_one_closes_before_wire_callback() -> None:
    raw_stream = ChunkStream((b"1234", b"5"))
    validator_calls: list[object] = []
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            peer=str(resolved.selected_ip),
            content=None,
            stream=raw_stream,
        )
    )
    client = make_transport(
        factory,
        limits=MeshTransportLimits(control_max_bytes=4),
    )
    with pytest.raises(MeshTransportUnavailable, match="mesh_response_limit_exceeded"):
        await client.request(
            "POST",
            "/mesh/v1/events",
            response_validator=lambda response: validator_calls.append(response),
        )
    assert raw_stream.closed
    assert validator_calls == []


@pytest.mark.asyncio
async def test_content_length_over_control_limit_is_rejected_before_read() -> None:
    raw_stream = ChunkStream((b"never-read",))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            peer=str(resolved.selected_ip),
            headers=((b"content-length", b"5"),),
            content=None,
            stream=raw_stream,
        )
    )
    client = make_transport(
        factory,
        limits=MeshTransportLimits(control_max_bytes=4),
    )
    with pytest.raises(MeshTransportUnavailable, match="mesh_response_limit_exceeded"):
        await client.request("POST", "/mesh/v1/events")
    assert raw_stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    (
        ((b"content-encoding", b"gzip"),),
        ((b"content-encoding", b"identity"),),
        ((b"content-length", b"1"), (b"transfer-encoding", b"chunked")),
        ((b"content-length", b"1"), (b"Content-Length", b"1")),
        ((b"transfer-encoding", b"gzip"),),
        ((b"transfer-encoding", b"chunked, gzip"),),
        ((b"trailer", b"x-mesh-signature"),),
    ),
)
async def test_compression_and_ambiguous_framing_are_rejected(
    headers: Sequence[tuple[bytes, bytes]],
) -> None:
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            peer=str(resolved.selected_ip),
            headers=headers,
            content=b"x",
        )
    )
    client = make_transport(factory)
    with pytest.raises(MeshTransportUnavailable):
        await client.request("POST", "/mesh/v1/events")


def test_folded_and_oversized_headers_fail_before_model_code() -> None:
    with pytest.raises(MeshTransportUnavailable):
        transport_module._validate_header_block(
            ((b"x", b"ok\r\n folded"),),
            max_fields=32,
            max_wire_bytes=8192,
        )
    with pytest.raises(MeshTransportUnavailable):
        transport_module._validate_header_block(
            tuple((f"x-{index}".encode(), b"v") for index in range(33)),
            max_fields=32,
            max_wire_bytes=8192,
        )
    with pytest.raises(MeshTransportUnavailable):
        transport_module._validate_header_block(
            ((b"x", b"v" * 4097),),
            max_fields=32,
            max_wire_bytes=8192,
        )


@pytest.mark.asyncio
async def test_request_header_duplicates_and_reserved_overrides_are_refused() -> None:
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(factory)
    with pytest.raises(MeshTransportUnavailable):
        await client.request(
            "POST",
            "/mesh/v1/events",
            headers=(("x-mesh", "one"), ("X-Mesh", "two")),
        )
    with pytest.raises(MeshTransportUnavailable):
        await client.request(
            "POST",
            "/mesh/v1/events",
            headers=(("Host", "evil.example"),),
        )
    for name in ("Authorization", "Cookie", "Transfer-Encoding", "Upgrade"):
        with pytest.raises(MeshTransportUnavailable):
            await client.request(
                "POST",
                "/mesh/v1/events",
                headers=((name, "secret"),),
            )
    assert factory.requests == []


@pytest.mark.asyncio
async def test_network_exception_traceback_is_sanitized() -> None:
    resolver = FakeResolver(OSError("https://internal.example:9443"))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(factory, resolver=resolver)
    with pytest.raises(MeshTransportUnavailable) as caught:
        await client.request("POST", "/mesh/v1/events")
    rendered = "".join(
        traceback.format_exception(
            type(caught.value),
            caught.value,
            caught.value.__traceback__,
        )
    )
    assert "internal.example" not in rendered


@pytest.mark.asyncio
async def test_successful_validator_only_receives_fully_bounded_response() -> None:
    calls: list[bytes] = []
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            peer=str(resolved.selected_ip),
            content=b"signed",
        )
    )
    client = make_transport(factory)
    result = await client.request(
        "POST",
        "/mesh/v1/events",
        response_validator=lambda response: calls.append(response.body) or "verified",
    )
    assert result == "verified"
    assert calls == [b"signed"]


@pytest.mark.asyncio
async def test_bootstrap_stream_never_aggregates_and_chunks_at_64k() -> None:
    raw_stream = ChunkStream((b"a" * 100_000, b"b" * 17))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            status=200,
            peer=str(resolved.selected_ip),
            content=None,
            stream=raw_stream,
        )
    )
    client = make_transport(
        factory,
        limits=MeshTransportLimits(bootstrap_max_bytes=100_017),
    )
    received: list[bytes] = []
    async with client.stream_bootstrap(
        "GET",
        "/mesh/v1/pair/pair-01/bootstrap",
    ) as stream:
        assert not hasattr(stream, "body")
        async for chunk in stream:
            assert len(chunk) <= BOOTSTRAP_CHUNK_MAX_BYTES
            received.append(chunk)
    assert b"".join(received) == b"a" * 100_000 + b"b" * 17
    assert raw_stream.closed


@pytest.mark.asyncio
async def test_empty_bootstrap_chunks_do_not_recurse_or_reach_consumer() -> None:
    raw_stream = ChunkStream((b"",) * 1_500 + (b"ok",))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            status=200,
            peer=str(resolved.selected_ip),
            content=None,
            stream=raw_stream,
        )
    )
    client = make_transport(factory)
    async with client.stream_bootstrap(
        "GET",
        "/mesh/v1/pair/pair-01/bootstrap",
    ) as stream:
        assert await anext(stream) == b"ok"


@pytest.mark.asyncio
async def test_bootstrap_single_chunk_plus_one_is_never_yielded() -> None:
    raw_stream = ChunkStream((b"12345678901",))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            status=200,
            peer=str(resolved.selected_ip),
            content=None,
            stream=raw_stream,
        )
    )
    client = make_transport(
        factory,
        limits=MeshTransportLimits(bootstrap_max_bytes=10),
    )
    async with client.stream_bootstrap(
        "GET",
        "/mesh/v1/pair/pair-01/bootstrap",
    ) as stream:
        with pytest.raises(MeshTransportUnavailable, match="mesh_response_limit_exceeded"):
            await anext(stream)
        assert stream.bytes_read == 0
    assert raw_stream.closed


@pytest.mark.asyncio
async def test_bootstrap_later_overflow_does_not_yield_offending_chunk() -> None:
    first = b"1" * BOOTSTRAP_CHUNK_MAX_BYTES
    raw_stream = ChunkStream((first, b"23456"))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            status=200,
            peer=str(resolved.selected_ip),
            content=None,
            stream=raw_stream,
        )
    )
    client = make_transport(
        factory,
        limits=MeshTransportLimits(
            bootstrap_max_bytes=BOOTSTRAP_CHUNK_MAX_BYTES + 4
        ),
    )
    async with client.stream_bootstrap(
        "GET",
        "/mesh/v1/pair/pair-01/bootstrap",
    ) as stream:
        assert await anext(stream) == first
        with pytest.raises(MeshTransportUnavailable, match="mesh_response_limit_exceeded"):
            await anext(stream)
        assert stream.bytes_read == BOOTSTRAP_CHUNK_MAX_BYTES
    assert raw_stream.closed


@pytest.mark.asyncio
async def test_bootstrap_content_length_plus_one_is_rejected_before_yield() -> None:
    raw_stream = ChunkStream((b"never-read",))
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            status=200,
            peer=str(resolved.selected_ip),
            headers=((b"content-length", b"11"),),
            content=None,
            stream=raw_stream,
        )
    )
    client = make_transport(
        factory,
        limits=MeshTransportLimits(bootstrap_max_bytes=10),
    )
    with pytest.raises(MeshTransportUnavailable, match="mesh_response_limit_exceeded"):
        async with client.stream_bootstrap(
            "GET",
            "/mesh/v1/pair/pair-01/bootstrap",
        ):
            pytest.fail("stream must not be yielded")
    assert raw_stream.closed


@pytest.mark.asyncio
async def test_total_timeout_includes_dns() -> None:
    resolver = FakeResolver((PUBLIC_IPV4,), delay=0.05)
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(peer=str(resolved.selected_ip))
    )
    client = make_transport(
        factory,
        resolver=resolver,
        timeouts=MeshTransportTimeouts(dns=1, control_total=0.01),
    )
    with pytest.raises(MeshTransportUnavailable, match="mesh_transport_timeout"):
        await client.request("POST", "/mesh/v1/events")
    assert factory.requests == []


@pytest.mark.asyncio
async def test_bootstrap_total_timeout_closes_stream() -> None:
    raw_stream = ChunkStream((b"x",), delay=0.05)
    factory = RecordingTransportFactory(
        lambda _request, resolved: mesh_response(
            status=200,
            peer=str(resolved.selected_ip),
            content=None,
            stream=raw_stream,
        )
    )
    client = make_transport(
        factory,
        timeouts=MeshTransportTimeouts(bootstrap_total=0.01),
    )
    with pytest.raises(MeshTransportUnavailable, match="mesh_transport_timeout"):
        async with client.stream_bootstrap(
            "GET",
            "/mesh/v1/pair/pair-01/bootstrap",
        ) as stream:
            await anext(stream)
    assert raw_stream.closed
