"""Strict destination validation and DNS pinning for Project Mesh.

The policy in this module is deliberately more conservative than
``ipaddress.is_global``.  Every entry in the IANA IPv4 and IPv6
Special-Purpose Address Space registries is denied, including entries which
IANA currently marks as globally reachable.  The checked-in snapshot keeps
the security decision deterministic and makes network access at runtime
unnecessary.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable
from urllib.parse import SplitResult, urlsplit

IANA_SPECIAL_REGISTRY_LAST_UPDATED = "2025-10-09"
IANA_SPECIAL_REGISTRY_VERIFIED = "2026-07-15"

# Exact Address Block rows from the IANA IPv4 Special-Purpose Address Space
# registry snapshot named above.  Overlapping rows are retained intentionally:
# the tuple is also an auditable transcription of the registry, not merely a
# minimal CIDR cover.
IANA_IPV4_SPECIAL_PURPOSE_CIDRS: tuple[str, ...] = (
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

# Exact Address Block rows from the IANA IPv6 Special-Purpose Address Space
# registry snapshot named above.
IANA_IPV6_SPECIAL_PURPOSE_CIDRS: tuple[str, ...] = (
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

# Additional ambiguity/transition/metadata ranges required by the Mesh threat
# model.  Some are wider than a registry row on purpose.  In particular,
# ``::/96`` denies deprecated IPv4-compatible text forms, while ``fec0::/10``
# and multicast are denied independently of CPython's evolving classifications.
EXTRA_IPV4_DENY_CIDRS: tuple[str, ...] = (
    "168.63.129.16/32",  # Azure WireServer / platform metadata endpoint.
)
EXTRA_IPV6_DENY_CIDRS: tuple[str, ...] = (
    "::/96",  # Deprecated IPv4-compatible IPv6 addresses.
    "fec0::/10",  # Deprecated site-local addresses.
    "ff00::/8",  # Multicast.
)

_IPV4_DENY_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in IANA_IPV4_SPECIAL_PURPOSE_CIDRS
) + tuple(ipaddress.ip_network(value) for value in EXTRA_IPV4_DENY_CIDRS)
_IPV6_DENY_NETWORKS = tuple(
    ipaddress.ip_network(value) for value in IANA_IPV6_SPECIAL_PURPOSE_CIDRS
) + tuple(ipaddress.ip_network(value) for value in EXTRA_IPV6_DENY_CIDRS)

_IPV6_ALLOCATED_GLOBAL_UNICAST = ipaddress.ip_network("2000::/3")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_PATH_SEGMENT = re.compile(r"[A-Za-z0-9._~-]{1,128}\Z")
_MAX_PUBLIC_URL_CHARS = 2048
_MAX_MESH_PATH_CHARS = 512
MAX_DNS_ADDRESSES = 16
DEFAULT_DNS_TIMEOUT_SECONDS = 2.0


class MeshDestinationError(ValueError):
    """Sanitized destination failure safe to expose to a caller."""

    def __init__(self, code: str = "invalid_mesh_destination") -> None:
        self.code = code
        super().__init__(code)


@runtime_checkable
class MeshResolver(Protocol):
    """Injectable asynchronous address resolver."""

    async def resolve(self, host: str, port: int) -> Sequence[str]: ...


class SystemMeshResolver:
    """Resolve TCP addresses without applying search-result truncation."""

    async def resolve(self, host: str, port: int) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        return tuple(info[4][0] for info in infos)


@dataclass(frozen=True, slots=True)
class MeshDestination:
    """Canonical Mesh origin, before per-request DNS resolution."""

    scheme: str
    host: str
    port: int
    authority: str
    canonical_url: str
    literal_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    test_loopback_http: bool = False

    @classmethod
    def parse(
        cls,
        url: str,
        *,
        test_allow_http_loopback: bool = False,
    ) -> "MeshDestination":
        """Parse a root-only ASCII HTTPS origin.

        Plain HTTP is available solely through the explicitly named test seam
        and then only for a canonical literal loopback address.  No production
        configuration path should pass that argument.
        """

        if type(test_allow_http_loopback) is not bool:
            raise MeshDestinationError()
        if type(url) is not str or not url or len(url) > _MAX_PUBLIC_URL_CHARS:
            raise MeshDestinationError()
        if not url.isascii() or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in url):
            raise MeshDestinationError()
        if "\\" in url or "%" in url or "?" in url or "#" in url:
            raise MeshDestinationError()
        if not (url.startswith("https://") or url.startswith("http://")):
            raise MeshDestinationError()

        try:
            split = urlsplit(url)
            port = split.port
        except ValueError:
            raise MeshDestinationError() from None

        if split.scheme not in {"https", "http"}:
            raise MeshDestinationError()
        if split.query or split.fragment or split.path not in {"", "/"}:
            raise MeshDestinationError()
        if not split.netloc or "@" in split.netloc:
            raise MeshDestinationError()
        if any("A" <= char <= "Z" for char in split.netloc):
            raise MeshDestinationError()
        if split.username is not None or split.password is not None:
            raise MeshDestinationError()

        raw_host = split.hostname
        if raw_host is None:
            raise MeshDestinationError()
        host, literal_ip = _canonical_host(raw_host, split)

        default_port = 443 if split.scheme == "https" else 80
        port = default_port if port is None else port
        if isinstance(port, bool) or not 1 <= port <= 65535:
            raise MeshDestinationError()

        test_loopback = False
        if split.scheme == "http":
            if (
                not test_allow_http_loopback
                or literal_ip is None
                or not literal_ip.is_loopback
            ):
                raise MeshDestinationError()
            test_loopback = True

        display_host = f"[{host}]" if isinstance(literal_ip, ipaddress.IPv6Address) else host
        authority = display_host if port == default_port else f"{display_host}:{port}"
        return cls(
            scheme=split.scheme,
            host=host,
            port=port,
            authority=authority,
            canonical_url=f"{split.scheme}://{authority}",
            literal_ip=literal_ip,
            test_loopback_http=test_loopback,
        )

    def revalidated(
        self,
        *,
        test_allow_http_loopback: bool = False,
    ) -> "MeshDestination":
        """Reparse and exactly match a preconstructed destination.

        ``MeshDestination`` remains a plain immutable value for transport and
        resolver hand-off, so callers can construct one without ``parse``.
        Every security boundary must therefore call this method and repeat the
        explicit HTTP-loopback test capability instead of trusting its fields.
        """

        if type(self) is not MeshDestination or type(test_allow_http_loopback) is not bool:
            raise MeshDestinationError()
        parsed = MeshDestination.parse(
            self.canonical_url,
            test_allow_http_loopback=test_allow_http_loopback,
        )
        actual = (
            self.scheme,
            self.host,
            self.port,
            self.authority,
            self.canonical_url,
            self.literal_ip,
            self.test_loopback_http,
        )
        expected = (
            parsed.scheme,
            parsed.host,
            parsed.port,
            parsed.authority,
            parsed.canonical_url,
            parsed.literal_ip,
            parsed.test_loopback_http,
        )
        if any(
            type(actual_value) is not type(expected_value)
            or actual_value != expected_value
            for actual_value, expected_value in zip(actual, expected, strict=True)
        ):
            raise MeshDestinationError()
        return parsed


@dataclass(frozen=True, slots=True)
class ResolvedMeshDestination:
    """A per-request DNS snapshot with one deterministic pinned address."""

    destination: MeshDestination
    addresses: tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]
    selected_ip: ipaddress.IPv4Address | ipaddress.IPv6Address

    @property
    def authority(self) -> str:
        return self.destination.authority

    @property
    def tls_server_name(self) -> str:
        return self.destination.host

    def numeric_url(self, path: str) -> str:
        path = validate_mesh_path(path)
        host = (
            f"[{self.selected_ip.compressed}]"
            if isinstance(self.selected_ip, ipaddress.IPv6Address)
            else self.selected_ip.compressed
        )
        return f"{self.destination.scheme}://{host}:{self.destination.port}{path}"


def _canonical_host(
    raw_host: str,
    split: SplitResult,
) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    if not raw_host or raw_host != raw_host.lower() or raw_host.endswith("."):
        raise MeshDestinationError()

    try:
        literal = ipaddress.ip_address(raw_host)
    except ValueError:
        literal = None

    if literal is not None:
        if raw_host != literal.compressed.lower():
            raise MeshDestinationError()
        if isinstance(literal, ipaddress.IPv6Address) and not split.netloc.startswith("["):
            raise MeshDestinationError()
        return literal.compressed.lower(), literal

    if ":" in raw_host or len(raw_host) > 253 or "." not in raw_host:
        raise MeshDestinationError()
    if all(char.isdigit() or char == "." for char in raw_host):
        # Prevent legacy octal/dword IPv4 interpretations in lower networking
        # layers.  Canonical IPv4 literals were handled above.
        raise MeshDestinationError()

    labels = raw_host.split(".")
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise MeshDestinationError()
    for label in labels:
        if label.startswith("xn--"):
            try:
                if label.encode("ascii").decode("idna").encode("idna").decode("ascii") != label:
                    raise MeshDestinationError()
            except UnicodeError:
                raise MeshDestinationError() from None
    return raw_host, None


def validate_mesh_path(path: str) -> str:
    """Validate an absolute canonical path under the reserved Mesh namespace."""

    if (
        not isinstance(path, str)
        or not path.isascii()
        or not 1 <= len(path) <= _MAX_MESH_PATH_CHARS
        or not path.startswith("/mesh/v1/")
        or path.endswith("/")
        or "//" in path
        or "\\" in path
        or "%" in path
        or "?" in path
        or "#" in path
    ):
        raise MeshDestinationError("invalid_mesh_path")
    segments = path.split("/")[1:]
    if any(
        segment in {"", ".", ".."} or not _PATH_SEGMENT.fullmatch(segment)
        for segment in segments
    ):
        raise MeshDestinationError("invalid_mesh_path")
    return path


def is_public_mesh_address(
    value: str | ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Return whether an address is acceptable for production Mesh traffic."""

    try:
        address = (
            value
            if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address))
            else ipaddress.ip_address(value)
        )
    except ValueError:
        return False

    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return False
    if any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.is_site_local or address not in _IPV6_ALLOCATED_GLOBAL_UNICAST:
            return False
        if any(address in network for network in _IPV6_DENY_NETWORKS):
            return False
    else:
        if any(address in network for network in _IPV4_DENY_NETWORKS):
            return False
    # This is intentionally the final, secondary guard.  The checked-in IANA
    # table above remains authoritative even where CPython says ``is_global``.
    return address.is_global


async def resolve_destination(
    destination: MeshDestination,
    *,
    resolver: MeshResolver | None = None,
    timeout_seconds: float = DEFAULT_DNS_TIMEOUT_SECONDS,
    max_addresses: int = MAX_DNS_ADDRESSES,
    test_allow_http_loopback: bool = False,
) -> ResolvedMeshDestination:
    """Resolve, validate every answer, and select one address to pin.

    This function must be called for every logical request.  No DNS result is
    cached here; a rebinding answer on a later request is therefore evaluated
    against the same fail-closed policy before any socket is opened.
    """

    if type(destination) is not MeshDestination:
        raise MeshDestinationError("mesh_resolution_failed")
    try:
        destination = destination.revalidated(
            test_allow_http_loopback=test_allow_http_loopback
        )
    except MeshDestinationError:
        raise MeshDestinationError("mesh_resolution_failed") from None

    if isinstance(max_addresses, bool) or not 1 <= max_addresses <= MAX_DNS_ADDRESSES:
        raise MeshDestinationError("mesh_resolution_failed")
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or timeout_seconds <= 0
    ):
        raise MeshDestinationError("mesh_resolution_failed")

    if destination.literal_ip is not None:
        raw_answers: Sequence[str] = (destination.literal_ip.compressed,)
    else:
        selected_resolver = resolver or SystemMeshResolver()
        try:
            raw_answers = await asyncio.wait_for(
                selected_resolver.resolve(destination.host, destination.port),
                timeout=float(timeout_seconds),
            )
        except (TimeoutError, OSError, socket.gaierror, UnicodeError, ValueError):
            raise MeshDestinationError("mesh_resolution_failed") from None

    if not raw_answers or len(raw_answers) > max_addresses:
        raise MeshDestinationError("mesh_resolution_failed")

    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw in raw_answers:
        if not isinstance(raw, str) or not raw or "%" in raw:
            raise MeshDestinationError("mesh_resolution_failed")
        try:
            address = ipaddress.ip_address(raw)
        except ValueError:
            raise MeshDestinationError("mesh_resolution_failed") from None
        if destination.test_loopback_http:
            if address != destination.literal_ip or not address.is_loopback:
                raise MeshDestinationError("mesh_resolution_failed")
        elif not is_public_mesh_address(address):
            raise MeshDestinationError("mesh_resolution_failed")
        parsed.append(address)

    unique = tuple(sorted(set(parsed), key=lambda item: (item.version, item.packed)))
    if not unique:
        raise MeshDestinationError("mesh_resolution_failed")
    return ResolvedMeshDestination(
        destination=destination,
        addresses=unique,
        selected_ip=unique[0],
    )


__all__ = [
    "DEFAULT_DNS_TIMEOUT_SECONDS",
    "EXTRA_IPV4_DENY_CIDRS",
    "EXTRA_IPV6_DENY_CIDRS",
    "IANA_IPV4_SPECIAL_PURPOSE_CIDRS",
    "IANA_IPV6_SPECIAL_PURPOSE_CIDRS",
    "IANA_SPECIAL_REGISTRY_LAST_UPDATED",
    "IANA_SPECIAL_REGISTRY_VERIFIED",
    "MAX_DNS_ADDRESSES",
    "MeshDestination",
    "MeshDestinationError",
    "MeshResolver",
    "ResolvedMeshDestination",
    "SystemMeshResolver",
    "is_public_mesh_address",
    "resolve_destination",
    "validate_mesh_path",
]
