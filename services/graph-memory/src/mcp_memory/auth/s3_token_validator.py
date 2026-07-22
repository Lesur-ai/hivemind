# -*- coding: utf-8 -*-
"""
S3TokenValidator — Hivemind unified token authority (Model B, P7-4 / ADR-0019).

LOCAL MODIFICATION to the vendored Graph Memory runtime (see THIRD_PARTY_NOTICES.md).

The embedded Graph Memory validates the SAME tokens as Hivemind by reading
Hivemind's token store ``_system/tokens.json`` from the shared S3 bucket, instead
of its own Neo4j ``(:Token)`` store. There is therefore ONE token system
end-to-end and no separate Graph Memory token to provision.

Design constraints honoured here:
- **Import-light.** The module top-level imports only stdlib. ``boto3`` and the
  GM settings are imported lazily inside the real-S3 read path, so this module
  (and its unit tests) import cleanly in environments without the full GM stack.
- **Signature mode mirrors Hivemind.** Hivemind GETs ``_system/tokens.json`` via
  SigV2 in its default ``dual`` mode (required for Dell ECS Cloud Temple);
  ``sigv4`` is opt-in for MinIO/AWS. The validator mirrors that — default
  ``dual`` — so it never bricks the reference deployment.
- **Hivemind schema.** Reads ``hash`` (``sha256:``+hex), ``name``,
  ``permissions``, ``revoked``, ``expires_at`` — NOT a ``allowed_resources`` key
  (that key does not exist in the persisted Hivemind token schema). Hivemind
  ``manage`` projects to GM ``write`` (GM only knows read/write/admin).
- **Versioned authority.** Accepts only token-store version 2 and validates the
  entire registry (not just the matching entry). Version 1 must be migrated by
  Hivemind's startup lifespan; missing, legacy, future or malformed state fails
  closed here so GM can never bypass that migration boundary. Version 2 also
  requires admin entries to carry ``space_ids=[]`` so a later downgrade cannot
  activate a dormant allowlist.
- **Mono-tenant.** The returned credential always carries ``memory_ids=[]`` so
  GM's ``memory_create`` token auto-add stays a no-op (never mutates the store).
- **Fail-closed.** Missing/unreadable store, no match, revoked, or expired ->
  ``None``. A short positive-only cache re-checks expiry on every hit and never
  caches negatives.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional


CURRENT_HIVEMIND_TOKENS_VERSION = 2
_CANONICAL_TOKEN_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CANONICAL_SPACE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_VALID_PERMISSIONS = frozenset({"read", "write", "manage", "admin"})


@dataclass
class TokenInfo:
    """GM-shaped token identity (duck-compatible with the middleware's reads).

    Mirrors the attribute surface the AuthMiddleware consumes:
    ``client_name``, ``permissions``, ``memory_ids``, ``token_hash``.
    """

    token_hash: str
    client_name: str
    permissions: list = field(default_factory=list)
    memory_ids: list = field(default_factory=list)
    is_active: bool = True
    expires_at: Optional[str] = None


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    raw = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class S3TokenValidator:
    """Validate a bearer token against Hivemind's S3 ``_system/tokens.json``."""

    def __init__(
        self,
        *,
        read_tokens_json: Optional[Callable[[], Awaitable[Optional[str]]]] = None,
        signature_mode: Optional[str] = None,
        clock: Optional[Callable[[], datetime]] = None,
        cache_ttl_seconds: float = 30.0,
    ) -> None:
        # Injected reader (tests) or None -> lazy real S3 read.
        self._read_tokens_json = read_tokens_json
        # Mirror Hivemind's S3_SIGNATURE_MODE; default 'dual' (Dell ECS SigV2 GET).
        self.signature_mode = (signature_mode or self._default_signature_mode()).lower()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # No positive authorization cache: the authoritative store is re-read and
        # re-validated on EVERY call, so a revoked/deleted token or an unreadable
        # store can never grant access (fail-closed). The param is accepted for
        # API stability only and does NOT gate authorization.
        self._cache_ttl = float(cache_ttl_seconds)

    # ----- configuration / lazy dependencies ----------------------------- #

    @staticmethod
    def _default_signature_mode() -> str:
        """Mirror Hivemind's ``S3_SIGNATURE_MODE`` exactly (single source of truth).

        The embedded GM shares Hivemind's ``.env``; reading the SAME env var
        guarantees the token-store GET uses the mode the operator set for the
        whole stack — ``dual`` (SigV2 GET, Dell ECS Cloud Temple) by default,
        ``sigv4`` when the operator runs MinIO/AWS. A separate GM-only knob could
        silently diverge and brick auth, so one is intentionally not used.
        """
        mode = os.getenv("S3_SIGNATURE_MODE", "dual").strip().lower()
        return mode if mode in ("dual", "sigv4") else "dual"

    async def _load_store(self) -> Optional[dict]:
        """Read + parse the Hivemind tokens store; None on any failure."""
        reader = self._read_tokens_json or self._read_tokens_json_from_s3
        try:
            payload = await reader()
        except Exception:
            return None
        if not payload:
            return None
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        version = data.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != CURRENT_HIVEMIND_TOKENS_VERSION
        ):
            # GM is a read-only consumer of the auth authority. It must never
            # migrate or reinterpret a legacy/future schema on its own.
            return None
        raw_tokens = data.get("tokens")
        if not isinstance(raw_tokens, list):
            return None
        seen_hashes: set[str] = set()
        for entry in raw_tokens:
            if not isinstance(entry, dict):
                return None
            token_hash = entry.get("hash")
            if not isinstance(token_hash, str) or not _CANONICAL_TOKEN_HASH_RE.fullmatch(
                token_hash
            ):
                return None
            if token_hash in seen_hashes:
                return None
            seen_hashes.add(token_hash)

            permissions = entry.get("permissions")
            if (
                not isinstance(permissions, list)
                or any(not isinstance(item, str) for item in permissions)
                or any(item not in _VALID_PERMISSIONS for item in permissions)
                or len(set(permissions)) != len(permissions)
            ):
                return None

            space_ids = entry.get("space_ids")
            if (
                not isinstance(space_ids, list)
                or any(not isinstance(item, str) for item in space_ids)
                or any(not _CANONICAL_SPACE_ID_RE.fullmatch(item) for item in space_ids)
                or len(set(space_ids)) != len(space_ids)
            ):
                return None
            if "admin" in permissions and space_ids:
                return None

            if "revoked" in entry and not isinstance(entry["revoked"], bool):
                return None
            if "expires_at" in entry and not (
                entry["expires_at"] is None or isinstance(entry["expires_at"], str)
            ):
                return None
            if "last_used_at" in entry and not (
                entry["last_used_at"] is None
                or isinstance(entry["last_used_at"], str)
            ):
                return None
            if any(
                field_name in entry and not isinstance(entry[field_name], str)
                for field_name in ("name", "email", "created_at")
            ):
                return None
        return data

    async def _read_tokens_json_from_s3(self) -> Optional[str]:
        """Default production reader (lazy boto3) honouring the mirrored mode.

        P12-3 (Hivemind #268): this independent per-call reader is external S3
        egress and follows the same uniform ``PROXY_URL`` rule as the
        document-storage clients (static per-client classification, no DNS/IP
        heuristics). Proxy failure keeps the existing fail-closed semantics:
        the caller swallows every exception and denies — a proxy outage can
        never widen authorization nor fall back to a direct connection.
        """
        import boto3  # lazy
        from botocore.config import Config  # lazy

        from ..config import get_settings  # lazy
        from ..core.egress import botocore_proxies  # lazy (import-light rule)

        settings = get_settings()
        sig = "s3" if self.signature_mode == "dual" else "s3v4"
        _proxies = botocore_proxies(getattr(settings, "proxy_url", None))
        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=getattr(settings, "s3_region_name", "us-east-1"),
            config=Config(
                signature_version=sig,
                s3={"addressing_style": "path"},
                **({"proxies": _proxies} if _proxies else {}),
            ),
        )
        key = getattr(settings, "hivemind_tokens_s3_key", "_system/tokens.json")
        obj = client.get_object(Bucket=settings.s3_bucket_name, Key=key)
        return obj["Body"].read().decode("utf-8")

    # ----- validation ---------------------------------------------------- #

    def _entry_live(self, entry: dict) -> bool:
        """Fail-closed liveness, re-checked on every call.

        Denies on: ``revoked is True``, a non-boolean (corrupt) ``revoked``
        value, a present-but-unparseable ``expires_at`` (corrupt), or a past
        expiry. A missing/empty ``expires_at`` means "no expiry".
        """
        revoked = entry.get("revoked", False)
        if revoked is True:
            return False
        if not isinstance(revoked, bool):
            return False  # corrupt/non-boolean revoked -> fail closed
        raw_exp = entry.get("expires_at")
        if raw_exp:  # present and non-empty
            exp = _parse_iso(raw_exp)
            if exp is None:
                return False  # unparseable expiry -> corrupt -> fail closed
            if exp <= self._clock():
                return False
        return True

    @staticmethod
    def _to_token_info(token_hash: str, entry: dict) -> TokenInfo:
        perms = list(entry.get("permissions", []))
        # Hivemind `manage` -> GM `write` (GM only knows read/write/admin).
        if "manage" in perms and "write" not in perms:
            perms.append("write")
        return TokenInfo(
            token_hash="hivemind:" + token_hash,
            client_name=entry.get("name", ""),
            permissions=perms,
            memory_ids=[],  # mono-tenant: never per-token scoping (NO-GO #8)
            is_active=True,
            expires_at=entry.get("expires_at"),
        )

    async def validate_token(self, raw_token: str) -> Optional[TokenInfo]:
        if not raw_token:
            return None
        token_hash = "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()

        # Re-read + re-validate the authoritative store on EVERY call: a revoked
        # or deleted token, or an unreadable/empty store, must never grant access
        # (fail-closed). No positive authorization is ever served from a cache.
        store = await self._load_store()
        if store is None:
            return None

        for entry in store["tokens"]:
            if not isinstance(entry, dict):
                continue
            # L'autorité Hivemind exige un hash stocké canonique complet.
            # Le moteur dérivé ne doit pas réinterpréter un bare hex.
            if entry.get("hash") == token_hash:
                if not self._entry_live(entry):
                    return None  # found but revoked/expired/corrupt -> deny
                return self._to_token_info(token_hash, entry)
        return None  # not found -> deny


_VALIDATOR: Optional[S3TokenValidator] = None


def get_s3_token_validator() -> S3TokenValidator:
    """Process-wide singleton used by the AuthMiddleware live path."""
    global _VALIDATOR
    if _VALIDATOR is None:
        _VALIDATOR = S3TokenValidator()
    return _VALIDATOR
