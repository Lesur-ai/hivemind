# -*- coding: utf-8 -*-
"""
Authenticated peer channel for Hivemind V1 (issue #4).

This module deliberately stops at the protocol boundary: it signs and verifies
peer events, checks freshness/version/membership/term, persists accepted events
idempotently, and hides delivery behind a small transport interface. It does not
perform bootstrap, queue ordering, token grants, bank commits or live-note
replication.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from pydantic import BaseModel, ConfigDict, Field

from .layout import PROTOCOL_VERSION
from .models import EventEnvelope, EventType, Member, MemberStatus, PeerScope
from .state import HivemindStateStore


PEER_KEY_PREFIX = "ed25519:"
PEER_SIGNATURE_ALGORITHM = "ed25519"
DEFAULT_REPLAY_WINDOW_SECONDS = 300


# Mapping EventType -> scope MINIMAL requis pour qu'un peer puisse poser cet
# event au journal (ADR-0016). Narrowing additif : l'absence du scope DÉNIE.
#
# - propose-class (TOKEN_CLAIM) : un peer qui propose une entrée de queue / tente
#   un claim exerce le droit ``propose`` ;
# - TOKEN_ACK : un ACK est un acte de RÉCEPTION/SERVICE (le peer acquitte un
#   event qu'il a persisté), PAS une proposition/claim. ADR-0016 range
#   receive/serve sous ``read``. Or l'all-ACK full-mesh attend un ACK de CHAQUE
#   membre ACTIVE (``expected_ack_node_ids``), y compris un membre read-only
#   légitime : exiger ``propose`` pour TOKEN_ACK rejetterait son ACK signé en
#   INSUFFICIENT_SCOPE et l'all-ACK ne convergerait JAMAIS (blocage permanent).
#   TOKEN_ACK -> ``read`` (plancher) ;
# - commit-class (BANK_COMMITTED + grant/release/tombstone/watermark, qui ne
#   sont émis que par le détenteur du token) : exige ``commit``. Le scope reste
#   une PRÉCONDITION amont — il n'accorde JAMAIS de bypass de
#   ``assert_commit_allowed()`` (ADR-0011), qui demeure la porte d'autorisation
#   séparée ;
# - tout le reste (membership / bootstrap / resync) reste opérateur / cycle de
#   vie : accepté comme aujourd'hui (plancher ``read``).
#
# NB : TOKEN_ACK n'est volontairement PAS listé — il retombe sur le plancher
# ``read`` via ``required_scope_for_event`` (default).
_EVENT_REQUIRED_SCOPE: dict[str, PeerScope] = {
    EventType.TOKEN_CLAIM.value: PeerScope.PROPOSE,
    EventType.BANK_COMMITTED.value: PeerScope.COMMIT,
    EventType.TOKEN_GRANTED.value: PeerScope.COMMIT,
    EventType.TOKEN_RELEASED.value: PeerScope.COMMIT,
    EventType.TOMBSTONE_RECORDED.value: PeerScope.COMMIT,
    EventType.WATERMARK_UPDATED.value: PeerScope.COMMIT,
}


def required_scope_for_event(event_type: "EventType | str") -> PeerScope:
    """Scope minimal exigé pour accepter un event d'un peer (plancher ``read``).

    Les events membership / bootstrap / resync (opérateur / cycle de vie) ne
    sont pas dans la table et retombent sur le plancher ``read`` : ils restent
    acceptés comme avant P5-9.
    """
    key = event_type.value if isinstance(event_type, EventType) else event_type
    return _EVENT_REQUIRED_SCOPE.get(key, PeerScope.READ)


class PeerErrorCode(str, Enum):
    """Machine-readable error taxonomy for Hivemind peer validation."""

    INVALID_SIGNATURE = "invalid_signature"
    STALE_TIMESTAMP = "stale_timestamp"
    WRONG_MEMBERSHIP_EPOCH = "wrong_membership_epoch"
    INCOMPATIBLE_PROTOCOL_VERSION = "incompatible_protocol_version"
    REPLAY_CONFLICT = "replay_conflict"
    UNKNOWN_PEER = "unknown_peer"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"
    PAYLOAD_HASH_MISMATCH = "payload_hash_mismatch"
    STALE_TERM = "stale_term"
    INVALID_KEY = "invalid_key"
    # Codes surface-opérateur (P5-4 / #12) — distinguent les trois issues d'un
    # déclencheur de récupération manuelle (eviction/resync) au-dessus de la
    # surface de statut read-only. Additifs, ne retirent aucun code transport.
    PERMISSION_DENIED = "permission_denied"  # appelant sans scope opérateur (evict/resync)
    PROTOCOL_BLOCKED = "protocol_blocked"  # la santé du hive interdit la mutation (fail-closed)
    READ_ONLY_ALLOWED = "read_only_allowed"  # lecture de statut autorisée (sentinelle chemin read)
    INSUFFICIENT_SCOPE = "insufficient_scope"


class PeerChannelError(RuntimeError):
    """Raised when a peer message fails closed."""

    def __init__(
        self,
        code: PeerErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": self.code.value,
            "message": str(self),
            "details": self.details,
        }


class _PeerBase(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        use_enum_values=True,
    )


class PeerKeyPair(_PeerBase):
    """
    Local-only Ed25519 material.

    The public key is safe to put in ``NodeIdentity.public_key`` and
    ``Member.public_key``. The private key is never written by the Hivemind
    state store and must live in operator-controlled local configuration.
    """

    algorithm: str = PEER_SIGNATURE_ALGORITHM
    public_key: str
    private_key: str = Field(repr=False)


class SignedPeerEvent(_PeerBase):
    """A signed Hivemind event ready for peer transport."""

    protocol_version: int = PROTOCOL_VERSION
    algorithm: str = PEER_SIGNATURE_ALGORITHM
    signer_node_id: str
    event_id: str
    signed_at: str
    membership_epoch: int = 0
    term: int = 0
    bank_version: int = -1
    payload_hash: str
    signature: str = ""
    event: EventEnvelope

    def model_post_init(self, __context: Any) -> None:  # type: ignore[override]
        if self.algorithm != PEER_SIGNATURE_ALGORITHM:
            raise ValueError(f"algorithm incompatible: {self.algorithm!r}")
        if self.event_id != self.event.event_id:
            raise ValueError("event_id top-level et event.event_id divergent")
        if self.signer_node_id != self.event.origin_node_id:
            raise ValueError("signer_node_id must match event.origin_node_id")
        if self.protocol_version != self.event.protocol_version:
            raise ValueError(
                "protocol_version top-level et event.protocol_version divergent"
            )
        if self.membership_epoch != self.event.membership_epoch:
            raise ValueError(
                "membership_epoch top-level et event.membership_epoch divergent"
            )
        if self.term != self.event.term:
            raise ValueError("term top-level et event.term divergent")
        if self.bank_version != self.event.bank_version:
            raise ValueError("bank_version top-level et event.bank_version divergent")


class PeerReceiveStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


class PeerReceiveResult(_PeerBase):
    status: PeerReceiveStatus
    event_id: str
    persisted: bool


class PeerDeliveryResult(_PeerBase):
    status: str = "sent"
    peer_node_id: str
    event_id: str


class PeerTransport(Protocol):
    """Small transport boundary used by later Hivemind layers."""

    async def send(self, peer: Member, message: SignedPeerEvent) -> PeerDeliveryResult:
        ...


class InMemoryPeerTransport:
    """Deterministic fake transport for unit tests and protocol harnesses."""

    def __init__(self, unavailable_peers: set[str] | None = None) -> None:
        self.unavailable_peers = set(unavailable_peers or set())
        self.inboxes: dict[str, list[SignedPeerEvent]] = {}

    async def send(self, peer: Member, message: SignedPeerEvent) -> PeerDeliveryResult:
        if peer.node_id in self.unavailable_peers:
            raise PeerChannelError(
                PeerErrorCode.TRANSPORT_UNAVAILABLE,
                f"transport indisponible pour peer {peer.node_id!r}",
                {"peer_node_id": peer.node_id},
            )
        self.inboxes.setdefault(peer.node_id, []).append(message)
        return PeerDeliveryResult(peer_node_id=peer.node_id, event_id=message.event_id)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_event_payload_hash(event: EventEnvelope) -> str:
    """SHA-256 over canonical JSON for deterministic cross-peer signatures."""

    return hashlib.sha256(
        _canonical_json_bytes(event.model_dump(mode="json"))
    ).hexdigest()


def _signature_payload(message: SignedPeerEvent) -> bytes:
    return _canonical_json_bytes(
        {
            "algorithm": message.algorithm,
            "bank_version": message.bank_version,
            "event_id": message.event_id,
            "membership_epoch": message.membership_epoch,
            "payload_hash": message.payload_hash,
            "protocol_version": message.protocol_version,
            "signed_at": message.signed_at,
            "signer_node_id": message.signer_node_id,
            "term": message.term,
        }
    )


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(value: str, expected_len: int) -> bytes:
    raw_value = value.removeprefix(PEER_KEY_PREFIX)
    try:
        raw = base64.urlsafe_b64decode(raw_value + "=" * (-len(raw_value) % 4))
    except Exception as e:
        raise PeerChannelError(
            PeerErrorCode.INVALID_KEY,
            "cle Ed25519 invalide: base64 illisible",
        ) from e
    if len(raw) != expected_len:
        raise PeerChannelError(
            PeerErrorCode.INVALID_KEY,
            f"cle Ed25519 invalide: {len(raw)} octets au lieu de {expected_len}",
        )
    return raw


def generate_peer_keypair() -> PeerKeyPair:
    """Generate a local-only Ed25519 keypair for a Hivemind node."""

    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_raw = private_key.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption(),
    )
    public_raw = public_key.public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return PeerKeyPair(
        public_key=PEER_KEY_PREFIX + _b64encode(public_raw),
        private_key=PEER_KEY_PREFIX + _b64encode(private_raw),
    )


def _load_private_key(encoded_private_key: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_b64decode(encoded_private_key, 32))


def _load_public_key(encoded_public_key: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_b64decode(encoded_public_key, 32))


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise PeerChannelError(
            PeerErrorCode.STALE_TIMESTAMP,
            f"timestamp invalide: {value!r}",
        ) from e
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class HivemindPeerChannel:
    """
    Validates, signs and delivers peer events for one local node and one space.

    Pair configuration is read from the shared Hivemind membership view
    (`Member.public_key`, `Member.endpoint`). The local private key is supplied
    by the caller and is intentionally absent from shared state.
    """

    def __init__(
        self,
        *,
        state: HivemindStateStore,
        local_node_id: str,
        private_key: str,
        transport: PeerTransport | None = None,
        replay_window_seconds: int = DEFAULT_REPLAY_WINDOW_SECONDS,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        if replay_window_seconds <= 0:
            raise ValueError("replay_window_seconds must be > 0")
        self._state = state
        self._local_node_id = local_node_id
        self._private_key = private_key
        self._transport = transport
        self._replay_window = timedelta(seconds=replay_window_seconds)
        self._clock = clock

    async def sign_event(
        self,
        event: EventEnvelope,
        *,
        signed_at: str | None = None,
    ) -> SignedPeerEvent:
        if event.origin_node_id != self._local_node_id:
            raise PeerChannelError(
                PeerErrorCode.UNKNOWN_PEER,
                "un noeud local ne peut signer que ses propres events",
                {
                    "local_node_id": self._local_node_id,
                    "origin_node_id": event.origin_node_id,
                },
            )
        payload_hash = canonical_event_payload_hash(event)
        unsigned = SignedPeerEvent(
            signer_node_id=self._local_node_id,
            event_id=event.event_id,
            signed_at=signed_at or _now_iso(),
            membership_epoch=event.membership_epoch,
            term=event.term,
            bank_version=event.bank_version,
            payload_hash=payload_hash,
            event=event,
        )
        signature = _load_private_key(self._private_key).sign(
            _signature_payload(unsigned)
        )
        return unsigned.model_copy(update={"signature": _b64encode(signature)})

    async def send(
        self,
        event: EventEnvelope,
        *,
        peer_node_id: str,
    ) -> PeerDeliveryResult:
        if self._transport is None:
            raise PeerChannelError(
                PeerErrorCode.TRANSPORT_UNAVAILABLE,
                "aucun transport Hivemind configure",
            )
        peer = await self._member(peer_node_id)
        message = await self.sign_event(event)
        try:
            return await self._transport.send(peer, message)
        except PeerChannelError:
            raise
        except Exception as e:
            raise PeerChannelError(
                PeerErrorCode.TRANSPORT_UNAVAILABLE,
                f"transport Hivemind indisponible: {e}",
                {"peer_node_id": peer_node_id},
            ) from e

    async def receive(self, message: SignedPeerEvent) -> PeerReceiveResult:
        await self._verify(message)

        existing = await self._state.get_event(message.event_id)
        if existing is not None:
            existing_hash = canonical_event_payload_hash(existing)
            if existing_hash != message.payload_hash:
                raise PeerChannelError(
                    PeerErrorCode.REPLAY_CONFLICT,
                    "event_id rejoue avec un payload different",
                    {"event_id": message.event_id},
                )
            return PeerReceiveResult(
                status=PeerReceiveStatus.DUPLICATE,
                event_id=message.event_id,
                persisted=False,
            )

        persisted = await self._state.append_event(message.event)
        if not persisted:
            # Race-safe fallback: append_event saw the ID after our read.
            existing = await self._state.get_event(message.event_id)
            if (
                existing is not None
                and canonical_event_payload_hash(existing) != message.payload_hash
            ):
                raise PeerChannelError(
                    PeerErrorCode.REPLAY_CONFLICT,
                    "event_id rejoue avec un payload different",
                    {"event_id": message.event_id},
                )
            return PeerReceiveResult(
                status=PeerReceiveStatus.DUPLICATE,
                event_id=message.event_id,
                persisted=False,
            )

        return PeerReceiveResult(
            status=PeerReceiveStatus.ACCEPTED,
            event_id=message.event_id,
            persisted=True,
        )

    async def _verify(self, message: SignedPeerEvent) -> None:
        if (
            message.protocol_version != PROTOCOL_VERSION
            or message.event.protocol_version != PROTOCOL_VERSION
        ):
            raise PeerChannelError(
                PeerErrorCode.INCOMPATIBLE_PROTOCOL_VERSION,
                "version protocole Hivemind incompatible",
                {
                    "expected": PROTOCOL_VERSION,
                    "received": message.protocol_version,
                    "event_received": message.event.protocol_version,
                },
            )

        computed_hash = canonical_event_payload_hash(message.event)
        if computed_hash != message.payload_hash:
            raise PeerChannelError(
                PeerErrorCode.PAYLOAD_HASH_MISMATCH,
                "payload_hash ne correspond pas a l'event canonique",
                {"event_id": message.event_id},
            )

        signed_at = _parse_iso(message.signed_at)
        now = self._clock().astimezone(timezone.utc)
        if abs(now - signed_at) > self._replay_window:
            raise PeerChannelError(
                PeerErrorCode.STALE_TIMESTAMP,
                "timestamp peer hors fenetre de rejeu",
                {
                    "event_id": message.event_id,
                    "signed_at": message.signed_at,
                    "replay_window_seconds": int(self._replay_window.total_seconds()),
                },
            )

        membership = await self._state.get_membership()
        if membership is None:
            raise PeerChannelError(
                PeerErrorCode.UNKNOWN_PEER,
                "membership Hivemind absente",
                {"signer_node_id": message.signer_node_id},
            )
        member = next(
            (m for m in membership.members if m.node_id == message.signer_node_id),
            None,
        )
        if (
            member is None
            or member.status != MemberStatus.ACTIVE.value
            or not member.public_key
        ):
            raise PeerChannelError(
                PeerErrorCode.UNKNOWN_PEER,
                "peer inconnu ou inactif",
                {"signer_node_id": message.signer_node_id},
            )

        # Droits scopés (ADR-0016) : map EventType -> scope minimal et DÉNIE si
        # le membre (narrowed) ne le détient pas. Les membres legacy/full ont
        # ``scopes is None`` -> jeu complet via ``effective_scopes()`` : ils
        # passent tous les checks (rétro-compat). Le scope est une PRÉCONDITION
        # amont : un peer ``commit`` passe ici mais n'obtient AUCUN bypass de
        # ``assert_commit_allowed()`` (ADR-0011), qui reste la porte séparée.
        required = required_scope_for_event(message.event.type)
        if not member.has_scope(required):
            raise PeerChannelError(
                PeerErrorCode.INSUFFICIENT_SCOPE,
                f"peer {message.signer_node_id!r} sans scope "
                f"{required.value!r} pour event {message.event.type!r}",
                {
                    "signer_node_id": message.signer_node_id,
                    "event_type": message.event.type,
                    "required_scope": required.value,
                    "granted_scopes": sorted(member.effective_scopes()),
                },
            )

        if message.membership_epoch != membership.epoch:
            raise PeerChannelError(
                PeerErrorCode.WRONG_MEMBERSHIP_EPOCH,
                "epoch membership incompatible",
                {
                    "expected": membership.epoch,
                    "received": message.membership_epoch,
                    "signer_node_id": message.signer_node_id,
                },
            )

        term = await self._state.get_term()
        if term is not None and message.term < term.term:
            raise PeerChannelError(
                PeerErrorCode.STALE_TERM,
                "term Hivemind stale",
                {
                    "current_term": term.term,
                    "received": message.term,
                    "signer_node_id": message.signer_node_id,
                },
            )

        try:
            signature = _b64decode(message.signature, 64)
            _load_public_key(member.public_key).verify(
                signature,
                _signature_payload(message),
            )
        except PeerChannelError:
            raise
        except InvalidSignature as e:
            raise PeerChannelError(
                PeerErrorCode.INVALID_SIGNATURE,
                "signature peer Ed25519 invalide",
                {"signer_node_id": message.signer_node_id},
            ) from e

    async def _member(self, node_id: str) -> Member:
        membership = await self._state.get_membership()
        if membership is None:
            raise PeerChannelError(
                PeerErrorCode.UNKNOWN_PEER,
                "membership Hivemind absente",
                {"peer_node_id": node_id},
            )
        member = next((m for m in membership.members if m.node_id == node_id), None)
        if (
            member is None
            or member.status != MemberStatus.ACTIVE.value
            or not member.public_key
        ):
            raise PeerChannelError(
                PeerErrorCode.UNKNOWN_PEER,
                "peer inconnu ou inactif",
                {"peer_node_id": node_id},
            )
        return member
