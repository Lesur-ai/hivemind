# -*- coding: utf-8 -*-
"""Signed Project Mesh peer client (P10-3, issue #191).

Composes a signed :class:`MeshRequestEnvelope` for each pairing operation and
sends it through a :class:`PeerSender`.  The sender is abstracted so production
uses the P10-2 :class:`HttpPeerTransport` (HTTPS, SSRF/rebinding defences, bounded
bodies, no redirects) while tests can route directly to a peer's in-process ASGI
router.

This client only *sends*; it never mutates local state.  Verifying the peer's
signed response envelope is the caller's (``pairing_service``) responsibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol, Sequence

from .secret import generate_pairing_nonce, generate_request_id
from .wire import (
    MESH_ROUTES,
    MeshHttpOperation,
    MeshRequestEnvelope,
    mesh_headers,
)

_HeaderList = Sequence[tuple[bytes, bytes]]


@dataclass(frozen=True, slots=True)
class PeerResponse:
    status_code: int
    headers: list[tuple[bytes, bytes]]
    body: bytes


class PeerSender(Protocol):
    async def send(
        self, method: str, path: str, *, headers: _HeaderList, body: bytes
    ) -> PeerResponse: ...


class MeshPairingClient:
    """Sign + send pairing requests to a single peer via an injected sender."""

    def __init__(
        self,
        sender: PeerSender,
        *,
        source_public_key: str,
        source_fingerprint: str,
        private_key,
        clock_ms: Callable[[], int],
        nonce_factory: Callable[[], str] = generate_pairing_nonce,
        request_id_factory: Callable[[], str] = generate_request_id,
    ) -> None:
        self._sender = sender
        self._source_public_key = source_public_key
        self._source_fingerprint = source_fingerprint
        self._private_key = private_key
        self._clock_ms = clock_ms
        self._nonce_factory = nonce_factory
        self._request_id_factory = request_id_factory

    async def _send(
        self,
        op: MeshHttpOperation,
        *,
        path: str,
        space_id: str,
        epoch: int,
        target_fingerprint: str,
        request_id: str,
        body: bytes,
    ) -> PeerResponse:
        envelope = MeshRequestEnvelope.create(
            op=op,
            path=path,
            space_id=space_id,
            source_public_key=self._source_public_key,
            source_fingerprint=self._source_fingerprint,
            target_fingerprint=target_fingerprint,
            membership_epoch=epoch,
            request_id=request_id,
            nonce=self._nonce_factory(),
            issued_at_ms=self._clock_ms(),
            body=body,
        )
        signature = envelope.sign(self._private_key)
        headers = list(mesh_headers(envelope, signature))
        return await self._sender.send(
            MESH_ROUTES[op].method, path, headers=headers, body=body
        )

    async def claim(
        self, *, space_id: str, epoch: int, target_fingerprint: str, pair_id: str, body: bytes
    ) -> PeerResponse:
        return await self._send(
            MeshHttpOperation.PAIR_CLAIM,
            path=MESH_ROUTES[MeshHttpOperation.PAIR_CLAIM].path_for(),
            space_id=space_id,
            epoch=epoch,
            target_fingerprint=target_fingerprint,
            request_id=pair_id,
            body=body,
        )

    async def status(
        self, *, space_id: str, epoch: int, target_fingerprint: str, pair_id: str
    ) -> PeerResponse:
        return await self._send(
            MeshHttpOperation.PAIR_STATUS,
            path=MESH_ROUTES[MeshHttpOperation.PAIR_STATUS].path_for(pair_id),
            space_id=space_id,
            epoch=epoch,
            target_fingerprint=target_fingerprint,
            request_id=pair_id,
            body=b"",
        )

    async def fetch_bootstrap(
        self, *, space_id: str, epoch: int, target_fingerprint: str, pair_id: str
    ) -> PeerResponse:
        return await self._send(
            MeshHttpOperation.PAIR_BOOTSTRAP,
            path=MESH_ROUTES[MeshHttpOperation.PAIR_BOOTSTRAP].path_for(pair_id),
            space_id=space_id,
            epoch=epoch,
            target_fingerprint=target_fingerprint,
            request_id=pair_id,
            body=b"",
        )

    async def ack(
        self, *, space_id: str, epoch: int, target_fingerprint: str, pair_id: str, body: bytes
    ) -> PeerResponse:
        return await self._send(
            MeshHttpOperation.PAIR_ACK,
            path=MESH_ROUTES[MeshHttpOperation.PAIR_ACK].path_for(pair_id),
            space_id=space_id,
            epoch=epoch,
            target_fingerprint=target_fingerprint,
            request_id=pair_id,
            body=body,
        )

    async def deliver_event(
        self,
        *,
        space_id: str,
        epoch: int,
        target_fingerprint: str,
        body: bytes,
        request_id: Optional[str] = None,
    ) -> PeerResponse:
        # ``request_id`` binds the wire envelope to the event body's request_id
        # (the router requires ``event.request_id == envelope.request_id``); pass
        # it when the caller already stamped the event body.
        return await self._send(
            MeshHttpOperation.EVENTS,
            path=MESH_ROUTES[MeshHttpOperation.EVENTS].path_for(),
            space_id=space_id,
            epoch=epoch,
            target_fingerprint=target_fingerprint,
            request_id=request_id or self._request_id_factory(),
            body=body,
        )


class HttpPeerSender:
    """Production :class:`PeerSender` backed by the P10-2 ``HttpPeerTransport``."""

    def __init__(self, transport, *, bootstrap_paths: Optional[frozenset[str]] = None) -> None:
        self._transport = transport
        self._bootstrap_paths = bootstrap_paths or frozenset()

    async def send(
        self, method: str, path: str, *, headers: _HeaderList, body: bytes
    ) -> PeerResponse:
        response = await self._transport.request(
            method, path, body=body, headers=tuple(headers)
        )
        return PeerResponse(
            status_code=response.status_code,
            headers=list(response.headers),
            body=response.body,
        )


__all__ = ["PeerResponse", "PeerSender", "MeshPairingClient", "HttpPeerSender"]
