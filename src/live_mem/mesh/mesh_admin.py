# -*- coding: utf-8 -*-
"""Admin control plane for Project Mesh pairing (P10-3, issue #191).

The operator console consumes ``/api/admin/mesh/*`` — **never** an MCP ``mesh_*``
tool (none is registered). This ASGI middleware sits behind ``AuthMiddleware``
(so it is never public) and enforces, for every request: an authenticated
``admin`` session, same-origin (Origin/Referer) proof for mutations, a
purpose-specific ``confirm`` flag, bounded input, and safe errors. Responses and
errors carry only identifiers/digests — never invitation secrets (except the
create response, which returns the one-time secret exactly once), private keys,
snapshots, or note contents.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

from ..auth.context import check_admin_permission, safe_error
from ..core.hivemind import hive_status
from .pairing_service import MeshPairingService, MeshPairingServiceError
from .pairing_state import MeshPairingState

_PREFIX = "/api/admin/mesh/"
_MAX_BODY_BYTES = 262_144
_SPACE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class MeshAdminMiddleware:
    """Handle ``/api/admin/mesh/*``; delegate everything else to ``app``."""

    def __init__(
        self, app: Any, pairing_service: MeshPairingService, *, process_lock: Any = None
    ) -> None:
        self._app = app
        self._service = pairing_service
        # The Mesh process-identity lock (the same one the peer router requires):
        # only the single leader process may serve Mesh mutations, so its in-process
        # store locks are the whole serialization. A pre-fork worker that inherited
        # the app but does not hold the lock must refuse — otherwise concurrent admin
        # requests across processes would not be serialized by those locks.
        self._process_lock = process_lock

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith(_PREFIX):
            await self._app(scope, receive, send)
            return
        # Recheck process-identity ownership per request (mirrors the peer router):
        # a worker that lost or never held the Mesh leader lock cannot mutate.
        if self._process_lock is not None and getattr(self._process_lock, "acquired", False) is not True:
            await self._json(send, {"status": "error", "message": "mesh not available on this process"}, status=503)
            return
        error = check_admin_permission()
        if error is not None:
            await self._json(send, error, status=403)
            return
        method = scope.get("method")
        action = str(scope.get("path", ""))[len(_PREFIX):]
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        try:
            if method == "GET" and action == "availability":
                # Deliberately lightweight capability probe for the admin
                # shell.  Process-lock ownership and admin permission were
                # already checked above; unlike /status this performs no
                # storage scan or source-readiness classification.
                await self._json(send, {"status": "ok"})
                return
            if method == "GET" and action == "status":
                await self._json(send, await self._status())
                return
            if method == "GET" and action.startswith("source-readiness/"):
                space_id = action[len("source-readiness/"):]
                if not _SPACE_ID_RE.fullmatch(space_id):
                    await self._json(
                        send,
                        {"status": "error", "message": "invalid space id"},
                        status=400,
                    )
                    return
                source = await self._service.inspect_source_eligibility(space_id)
                await self._json(send, {"status": "ok", "source": source})
                return
            if method == "GET" and action.startswith("members/"):
                space_id = action[len("members/"):]
                if not _SPACE_ID_RE.fullmatch(space_id):
                    await self._json(send, {"status": "error", "message": "invalid space id"}, status=400)
                    return
                await self._json(send, await self._members(space_id))
                return
            if method != "POST":
                await self._json(send, {"status": "error", "message": "not found"}, status=404)
                return
            if not _same_origin(headers):
                await self._json(send, {"status": "error", "message": "cross-origin refused"}, status=403)
                return
            body = await self._read_body(receive)
            data = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(data, dict):
                await self._json(send, {"status": "error", "message": "invalid body"}, status=400)
                return
            if data.get("confirm") is not True:
                await self._json(send, {"status": "error", "message": "explicit confirmation required"}, status=400)
                return
            result = await self._dispatch(action, data)
            await self._json(send, {"status": "ok", **result})
        except MeshPairingServiceError as exc:
            if exc.code == "source_state_changed":
                status = 409
            elif exc.code in {
                "mesh_status_inventory_unavailable",
                "mesh_status_inventory_too_large",
            }:
                status = 503
            else:
                status = 400
            await self._json(
                send,
                {"status": "error", "code": exc.code, "message": exc.safe_message},
                status=status,
            )
        except (KeyError, ValueError, TypeError):
            await self._json(send, {"status": "error", "message": "invalid request"}, status=400)
        except Exception as exc:  # pragma: no cover - defensive
            await self._json(send, safe_error(exc, "mesh_admin"), status=500)

    async def _dispatch(self, action: str, data: dict) -> dict:
        if action == "prepare-source":
            space_id, expected_state_token = _prepare_source_fields(data)
            return await self._service.prepare_source(
                space_id,
                expected_state_token=expected_state_token,
                quiesced=True,
            )
        if action == "invitation":
            out = await self._service.create_invitation(
                _req_str(data, "space_id"), requested_scopes=_scopes(data)
            )
            # The one-time secret + signed invitation are shown to the operator
            # exactly once (base64url); the digest-only session persists.
            return {
                "pair_id": out["pair_id"],
                "secret": out["secret"],
                "invitation": base64.urlsafe_b64encode(out["invitation_bytes"]).decode("ascii"),
                "source_endpoint": out["source_endpoint"],
                "source_fingerprint": out["source_fingerprint"],
            }
        if action == "accept":
            (
                invitation,
                target_space_id,
                secret,
                source_endpoint,
                scopes,
            ) = _accept_fields(data)
            invitation_bytes = base64.urlsafe_b64decode(invitation.encode("ascii"))
            return await self._service.accept_invitation(
                invitation_bytes,
                target_space_id,
                secret=secret,
                source_endpoint=source_endpoint,
                requested_scopes=scopes,
                quiesced=True,
            )
        if action == "approve":
            return await self._service.approve(_req_str(data, "pair_id"))
        if action == "enroll":
            # Action 3 (target side): drive bootstrap import + final ACK + await
            # activation. Serialized and re-entrant inside the service.
            return await self._service.run_target_enrollment(_req_str(data, "pair_id"))
        if action == "resync":
            # Corrupt-import recovery (target side): teardown-to-blank + re-import
            # + re-drive to active, consuming the signed blocked-recovery evidence.
            return await self._service.resync(_req_str(data, "pair_id"))
        if action == "recover-orphaned-reservation":
            # Compatibility recovery for the old reserve-only crash prefix.
            # It is deliberately explicit/operator-gated rather than allowing
            # accept() to infer identity from unbound caller input.
            return await self._service.recover_orphaned_target_reservation(
                _req_str(data, "pair_id"),
                space_id=_req_str(data, "space_id"),
                operator=_req_str(data, "operator"),
            )
        if action == "abandon":
            # Target-side give-up: after the source has evicted/cancelled this
            # pairing, release the target's OWN reservation + teardown + cancel.
            return await self._service.abandon(_req_str(data, "pair_id"))
        if action == "resume":
            return await self._service.resume(_req_str(data, "pair_id"))
        if action == "cancel":
            return await self._service.cancel(_req_str(data, "pair_id"))
        if action == "evict":
            return await self._service.evict(
                _req_str(data, "pair_id"), operator=_req_str(data, "operator"), reason=str(data.get("reason", ""))
            )
        if action == "force-evict-member":
            # Operator-forced removal of a DEAD active target (epoch-advancing
            # member eviction) when resume cannot converge and pairing evict
            # correctly refuses a promoted member. The operator asserts the node is
            # dead (split-brain if it is actually alive).
            return await self._service.force_evict_member(
                _req_str(data, "pair_id"), operator=_req_str(data, "operator"), reason=str(data.get("reason", ""))
            )
        raise MeshPairingServiceError("unknown_action", "unknown admin mesh action")

    async def _status(self) -> dict:
        sessions, pairings_truncated = (
            await self._service.store.list_sessions_diagnostic(
                max_sessions=self._service.STATUS_MAX_SESSIONS
            )
        )
        source_readiness_unavailable = False
        source_readiness_truncated = False
        source_readiness_unavailable_reason = ""
        try:
            source_readiness = await self._service.list_source_eligibility()
        except MeshPairingServiceError as exc:
            if exc.code not in {
                "mesh_status_inventory_unavailable",
                "mesh_status_inventory_too_large",
            }:
                raise
            # Source readiness is an enrichment of the established admin status
            # surface. Inventory failure must not hide pairing lifecycle or
            # recovery controls; return no partial eligibility projection and an
            # explicit non-authoritative diagnostic instead.
            source_readiness = []
            source_readiness_unavailable = True
            source_readiness_truncated = (
                exc.code == "mesh_status_inventory_too_large"
            )
            source_readiness_unavailable_reason = exc.code
        eligible_spaces = [
            source["space_id"]
            for source in source_readiness
            if source.get("can_create_invitation") is True
        ]
        pairings = []
        for s in sessions:
            entry = {
                "pair_id": s.pair_id,
                "role": s.role,
                "state": s.state,
                "space_id": s.space_id,
                "source_fingerprint": s.source_fingerprint,
                "source_endpoint": s.source_endpoint,
                "target_fingerprint": s.target_fingerprint,
                "target_endpoint": s.target_endpoint,
                "granted_scopes": list(s.granted_scopes),
                "created_at_ms": s.created_at_ms,
                "updated_at_ms": s.updated_at_ms,
                "expires_at_ms": s.expires_at_ms,
                "last_error": s.last_error,
                # Progressive-disclosure diagnostics only (design pack §4) —
                # never rendered in the normal three-action flow.
                "base_epoch": s.base_epoch,
                "invitation_digest": s.invitation_digest,
                "claim_digest": s.claim_digest,
                "approval_digest": s.approval_digest,
                "bootstrap_manifest_digest": s.bootstrap_manifest_digest,
                "activation_event_id": s.activation_event_id,
            }
            if s.state == MeshPairingState.BLOCKED_RECOVERY.value:
                next_action, phase = await self._blocked_recovery_hint(s.pair_id)
                if next_action is not None:
                    entry["next_action"] = next_action
                    entry["phase"] = phase
            pairings.append(entry)
        config = self._service._config
        return {
            "status": "ok",
            "enabled": True,
            # Same signal the middleware already 503s mutations on — surfaced
            # for the read-only status display too (design pack §6).
            "healthy": self._process_lock is None or getattr(self._process_lock, "acquired", False) is True,
            "display_name": config.display_name,
            "public_url": config.public_url,
            "fingerprint": config.fingerprint,  # public identifier only
            "pairings": pairings,
            "pairings_truncated": pairings_truncated,
            # One server-owned predicate feeds both fields. ``eligible_spaces``
            # is only an id projection, never an independently drifting check.
            "source_readiness": source_readiness,
            "eligible_spaces": eligible_spaces,
            "source_readiness_unavailable": source_readiness_unavailable,
            "source_readiness_truncated": source_readiness_truncated,
            "source_readiness_unavailable_reason": (
                source_readiness_unavailable_reason
            ),
        }

    async def _blocked_recovery_hint(self, pair_id: str) -> tuple[Optional[str], Optional[str]]:
        # Best-effort diagnostic only: resume()/evict() re-verify this evidence
        # themselves before acting, so a failure here never masks a real check
        # — it only means the admin console shows a generic recovery choice
        # instead of the recorded one.
        try:
            signed = await self._service.store.get_evidence(pair_id)
            if signed is None:
                return None, None
            signed.verify(self._service._config.public_key)
        except Exception:
            return None, None
        return signed.evidence.next_action, signed.evidence.phase

    async def _members(self, space_id: str) -> dict:
        status = await hive_status(self._service._storage_factory(), space_id)
        sessions, sessions_truncated = (
            await self._service.store.list_sessions_diagnostic(
                max_sessions=self._service.STATUS_MAX_SESSIONS
            )
        )
        peer_info: dict[str, dict[str, Any]] = {}
        for s in sessions:
            for fingerprint, endpoint, scopes in (
                (s.source_fingerprint, s.source_endpoint, None),
                (s.target_fingerprint, s.target_endpoint, s.granted_scopes),
            ):
                if not fingerprint or ":" not in fingerprint:
                    continue
                node_id = fingerprint.split(":", 1)[1]
                peer_info[node_id] = {
                    "fingerprint": fingerprint,
                    "endpoint": endpoint,
                    "scopes": list(scopes) if scopes else None,
                }
        members = []
        for peer in status.get("peers", []):
            if peer.get("status") != "active":
                continue
            extra = peer_info.get(peer.get("node_id", ""), {})
            members.append(
                {
                    "node_id": peer.get("node_id", ""),
                    "display_name": peer.get("display_name") or "",
                    "endpoint": extra.get("endpoint") or peer.get("endpoint") or "",
                    "fingerprint": extra.get("fingerprint") or "",
                    "scopes": extra.get("scopes"),
                }
            )
        return {
            "status": "ok",
            "space_id": space_id,
            "membership_epoch": status.get("membership_epoch"),
            "members": members,
            "pairing_metadata_truncated": sessions_truncated,
        }

    async def _read_body(self, receive: Any) -> bytes:
        chunks = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunks.extend(message.get("body", b""))
            if len(chunks) > _MAX_BODY_BYTES:
                raise ValueError("admin body too large")
            if not message.get("more_body", False):
                break
        return bytes(chunks)

    async def _json(self, send: Any, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _req_str(data: dict, key: str) -> str:
    value = data.get(key)
    if type(value) is not str or not value:
        raise MeshPairingServiceError("invalid_field", f"missing or invalid {key!r}")
    return value


def _prepare_source_fields(data: dict) -> tuple[str, str]:
    """Validate the closed, purpose-specific prepare-source request shape."""

    expected_fields = {
        "space_id",
        "confirm",
        "quiesced",
        "expected_state_token",
    }
    if set(data) != expected_fields:
        raise MeshPairingServiceError(
            "invalid_field", "invalid source preparation request"
        )
    if data.get("confirm") is not True:
        raise MeshPairingServiceError(
            "confirmation_required", "explicit confirmation required"
        )
    if data.get("quiesced") is not True:
        raise MeshPairingServiceError(
            "quiescence_required", "writers-quiesced confirmation is required"
        )
    return _req_str(data, "space_id"), _req_str(data, "expected_state_token")


def _accept_fields(data: dict) -> tuple[str, str, str, str, tuple[str, ...]]:
    """Validate the closed, purpose-specific target-acceptance request shape."""

    expected_fields = {
        "confirm",
        "invitation",
        "quiesced",
        "scopes",
        "secret",
        "source_endpoint",
        "target_space_id",
    }
    if set(data) != expected_fields:
        raise MeshPairingServiceError(
            "invalid_field", "invalid target acceptance request"
        )
    if data.get("confirm") is not True:
        raise MeshPairingServiceError(
            "confirmation_required", "explicit confirmation required"
        )
    if data.get("quiesced") is not True:
        raise MeshPairingServiceError(
            "quiescence_required", "writers-quiesced confirmation is required"
        )
    return (
        _req_str(data, "invitation"),
        _req_str(data, "target_space_id"),
        _req_str(data, "secret"),
        _req_str(data, "source_endpoint"),
        _scopes(data),
    )


def _scopes(data: dict) -> tuple[str, ...]:
    scopes = data.get("scopes", ["read"])
    if type(scopes) is not list or any(type(s) is not str for s in scopes):
        raise MeshPairingServiceError("invalid_scopes", "invalid scopes")
    return tuple(scopes)


def _same_origin(headers: dict) -> bool:
    # Defense-in-depth beyond the SameSite=Strict auth cookie: a mutating request
    # must carry an Origin (or Referer) whose host matches the request Host.
    host = headers.get(b"host")
    if host is None:
        return False
    origin = headers.get(b"origin")
    if origin is not None:
        return origin.split(b"//", 1)[-1].split(b"/", 1)[0] == host
    referer = headers.get(b"referer")
    if referer is not None:
        return referer.split(b"//", 1)[-1].split(b"/", 1)[0] == host
    return False  # no Origin/Referer on a state-changing request -> refuse


__all__ = ["MeshAdminMiddleware"]
