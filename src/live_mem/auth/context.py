# -*- coding: utf-8 -*-
"""
Helpers d'authentification basés sur contextvars.

Le middleware ASGI injecte les infos du token dans les contextvars.
Les outils MCP appellent check_access(), check_write_permission(),
check_manage_permission() et check_admin_permission() pour vérifier
les permissions sans dépendre du framework HTTP.

Architecture :
    Middleware ASGI → injecte current_token_info (contextvar)
    Outils MCP → appellent check_xxx() → lisent le contextvar

Voir AUTH_AND_COLLABORATION.md pour la matrice des permissions.

4 niveaux de permission (hiérarchie inclusive) :
    admin ⊃ manage ⊃ write ⊃ read

    - read    (🔑) : lecture des espaces et notes
    - write   (✏️) : écriture de notes + consolidation de ses propres notes
    - manage  (🔧) : maintenance bank (write/delete/repair/compact), space delete,
                      update rules, backup restore/delete
    - admin   (👑) : gestion tokens, GC, accès total sans restriction de space
"""

import re
from contextvars import ContextVar
from typing import Any, Mapping, Optional, Protocol, runtime_checkable


# Request-scoped identity attached by ``AuthMiddleware`` after bearer
# validation.  MCP Streamable HTTP copies the Starlette ``Request`` for each
# POST into the SDK request context, including ``scope["state"]``.  Unlike the
# session task's contextvars, this state therefore follows the current call.
REQUEST_TOKEN_INFO_STATE_KEY = "live_mem.token_info"

_REQUEST_TOKEN_SCALAR_FIELDS = (
    "type",
    "client_name",
    "token_hash",
    "email",
    "created_at",
    "expires_at",
    "last_used_at",
)

# VULN-08 fix : regex de validation du space_id, appliquée dans check_access()
# Empêche l'utilisation de space_ids malveillants (_system, _backups, ../)
_SPACE_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# ─────────────────────────────────────────────────────────────
# Context variable injectée par le middleware AuthMiddleware
# ─────────────────────────────────────────────────────────────
# Contient un dict avec les champs :
#   - client_name: str (nom du token)
#   - permissions: list[str] (["read"], ["read", "write"], etc.)
#   - allowed_resources: list[str] (space_ids autorisés, [] = aucun pour non-admin)
# Ou None si pas de token / token invalide.
current_token_info: ContextVar[Optional[dict]] = ContextVar(
    "current_token_info", default=None
)

# ─────────────────────────────────────────────────────────────
# Fresh token store — contourne le bug des contextvars MCP
# ─────────────────────────────────────────────────────────────
# Le MCP Streamable HTTP crée un task anyio par session. Les tool
# handlers s'exécutent dans ce task, qui a une COPIE FIGÉE du contexte
# asyncio de l'initialisation. Les contextvars du middleware (mis à jour
# à chaque POST) ne sont donc PAS visibles par les tools.
#
# Ce store global est mis à jour par le middleware à chaque requête HTTP,
# et lu par les fonctions check_xxx() pour obtenir les données fraîches
# (permissions, space_ids) même depuis le session task.
_fresh_token_store: dict[str, dict] = {}

# RA-1 fix : jeu de hashes invalidés (révocation / rescope). Rend le chemin de
# LECTURE fail-closed. Sans lui, ``_get_effective_token_info`` retombait sur le
# contextvar FIGÉ (potentiellement élevé) dès qu'un token était retiré du fresh
# store — une opération longue en cours de session task continuait alors avec
# les anciennes permissions (fenêtre de persistance de privilège). Un hash est
# retiré du tombstone dès qu'il est re-validé (S3 frais) par le middleware.
_invalidated_token_hashes: set[str] = set()


def request_token_info_snapshot(token_info: Mapping[str, Any]) -> dict[str, Any]:
    """Return a request-local copy without any bearer secret.

    Token validation already supplies these fields.  Copying the two list
    fields prevents later store mutations from changing an in-flight request's
    identity, while the explicit allowlist prevents accidental propagation of
    future secret-bearing fields through ASGI state.
    """

    snapshot = {
        field: token_info.get(field)
        for field in _REQUEST_TOKEN_SCALAR_FIELDS
        if field in token_info
    }
    permissions = token_info.get("permissions")
    snapshot["permissions"] = (
        list(permissions)
        if isinstance(permissions, (list, tuple))
        and all(isinstance(item, str) for item in permissions)
        else []
    )
    allowed_resources = token_info.get("allowed_resources")
    snapshot["allowed_resources"] = (
        list(allowed_resources)
        if isinstance(allowed_resources, (list, tuple))
        and all(isinstance(item, str) for item in allowed_resources)
        else []
    )
    return snapshot


def request_token_info_from_request(request: Any) -> Optional[dict[str, Any]]:
    """Read the identity attached to a Starlette request, failing closed."""

    scope = getattr(request, "scope", None)
    if not isinstance(scope, Mapping):
        return None
    state = scope.get("state")
    if not isinstance(state, Mapping):
        return None
    token_info = state.get(REQUEST_TOKEN_INFO_STATE_KEY)
    if not isinstance(token_info, Mapping):
        return None
    return request_token_info_snapshot(token_info)


def get_mcp_request_token_info() -> tuple[bool, Optional[dict[str, Any]]]:
    """Return ``(has_mcp_context, request_identity)`` for the current call.

    The boolean distinguishes an MCP request whose identity is missing (which
    must fail closed) from a direct admin-console call outside the MCP SDK,
    where existing contextvar/fresh-store handling remains authoritative.
    """

    try:
        from mcp.server.lowlevel.server import request_ctx

        context = request_ctx.get()
    except LookupError:
        return False, None

    request = getattr(context, "request", None)
    if request is None:
        return True, None
    return True, request_token_info_from_request(request)


def update_fresh_token(token_info: dict) -> None:
    """Met à jour le store global avec les infos fraîches du token.

    Appelé par AuthMiddleware à chaque requête HTTP validée.
    Le token_hash sert de clé (un slot par token distinct).

    LM2-08 fix (doc) : le bootstrap key n'a pas de ``token_hash`` (il
    n'est pas stocké dans ``_system/tokens.json``). Ses infos sont donc
    figées dans le contextvar et ne sont jamais publiées ici — c'est
    volontaire et inoffensif (le bootstrap est toujours admin total).
    """
    token_hash = token_info.get("token_hash")
    if token_hash:
        _fresh_token_store[token_hash] = token_info
        # RA-1 fix : un token re-validé (S3 frais, non révoqué) par le middleware
        # est de nouveau légitime — on lève le tombstone posé par un éventuel
        # update_token/rescope antérieur. Un token RÉVOQUÉ, lui, échoue à
        # validate_token (401) et n'atteint jamais cette ligne : son tombstone
        # persiste, ce qui est le comportement fail-closed voulu.
        _invalidated_token_hashes.discard(token_hash)


def invalidate_token_in_store(token_hash: str) -> None:
    """
    LM2-07 fix : retire un token du store global (révocation effective).

    Doit être appelé par TokenService après revoke_token, delete_token,
    purge_tokens, update_token, bulk_update_tokens. Sans cela, une
    opération longue (consolidation 5 min, push graph 10 min) qui aurait
    démarré juste avant la révocation continuerait à voir l'ancien
    ``permissions``/``allowed_resources`` via ``_get_effective_token_info``
    et pourrait persister une élévation de privilège jusqu'à la fin de
    l'opération.

    Idempotent : no-op si le token n'est pas dans le store (cas typique
    des tokens jamais utilisés depuis le démarrage du process).

    Note : l'invalidation ne casse pas une requête HTTP en cours (le
    contextvar reste figé pour la durée du handler), mais toute requête
    suivante de l'agent obtiendra un 401 sur le pipeline normal.

    RA-1 fix : on marque aussi le hash comme invalidé (tombstone) pour que
    ``_get_effective_token_info`` échoue FERMÉ côté lecture — sans quoi un
    miss du fresh store retombait sur le contextvar figé (droits anciens).
    """
    _fresh_token_store.pop(token_hash, None)
    _invalidated_token_hashes.add(token_hash)


def get_effective_token_info() -> Optional[dict]:
    """Retourne le token_info le plus frais disponible.

    Le contextvar peut être stale (figé à l'initialisation de la session
    MCP Streamable HTTP). Le store global est mis à jour par le middleware
    à chaque requête HTTP et contient les données fraîches.

    Priorité : identité de la requête MCP courante > store global (frais)
    > contextvar (potentiellement stale). Un contexte MCP présent mais sans
    identité de requête valide échoue fermé au lieu de retomber sur la session.
    """
    has_mcp_context, request_token_info = get_mcp_request_token_info()
    if has_mcp_context:
        # Per-request transport metadata is newer than the session task's
        # contextvar and follows bearer changes on an existing MCP session.
        return request_token_info

    token_info = current_token_info.get()
    if token_info is None:
        return None

    token_hash = token_info.get("token_hash")

    # RA-1 fix : fail-closed. Si le token a été invalidé (révoqué/rescopé) pendant
    # la session, ne PAS retomber sur le contextvar figé — refuser explicitement.
    if token_hash and token_hash in _invalidated_token_hashes:
        return None

    # Rafraîchir depuis le store global si disponible
    if token_hash and token_hash in _fresh_token_store:
        return _fresh_token_store[token_hash]

    return token_info


def _get_effective_token_info() -> Optional[dict]:
    """Compatibilité interne pour l'ancien nom privé.

    Les nouveaux chemins d'autorisation actor-aware utilisent le nom public
    :func:`get_effective_token_info`; les appels historiques restent valides.
    """
    return get_effective_token_info()


def _evaluate_access(
    token_info: Optional[dict], resource_id: str
) -> Optional[dict]:
    """
    Shared evaluator for space-allowlist + admin-bypass access.

    Both :func:`check_access` (ambient: reads ``current_token_info``
    via :func:`_get_effective_token_info`) and
    :meth:`MonoTenantSpaceAllowlistProvider.authorize` (explicit:
    supplied ``identity``) route through this helper, so the two
    paths cannot diverge. The function takes the ``token_info`` dict
    explicitly — there is no implicit fallback to the ambient
    contextvar.

    Args:
        token_info: Token info dict, or ``None``.
        resource_id: ID de l'espace à vérifier.

    Returns:
        ``None`` if access is allowed; otherwise a
        ``{"status": "error", "message": ...}`` dict.
    """
    # Pas de token → accès refusé
    if token_info is None:
        return {"status": "error", "message": "Authentification requise"}

    # VULN-08 fix : valider le format du space_id AVANT de vérifier les permissions
    # Empêche les tentatives de path traversal via _system, _backups, etc.
    if not _SPACE_ID_REGEX.match(resource_id):
        return {
            "status": "error",
            "message": f"Identifiant d'espace invalide : '{resource_id}'",
        }

    # Admin → accès total (pas de restriction par espace)
    if "admin" in token_info.get("permissions", []):
        return None

    # Vérifier que l'espace est dans la liste autorisée
    # IMPORTANT (v1.5.0) : space_ids=[] signifie "aucun accès" pour les non-admin.
    # Un token fraîchement créé n'a accès à aucun espace. Seul un manager
    # autorisé peut créer un espace ou inviter le token dans un espace.
    allowed = token_info.get("allowed_resources", [])
    if not allowed or resource_id not in allowed:
        return {
            "status": "error",
            "message": f"Accès refusé à l'espace '{resource_id}'",
        }

    return None  # OK


def check_access(resource_id: str) -> Optional[dict]:
    """
    Vérifie que le token courant a accès à la ressource (espace).

    Un token peut être restreint à certains space_ids.
    Si allowed_resources est vide → aucun accès pour un token non-admin.

    Utilise _get_effective_token_info() pour contourner le bug des
    contextvars stale dans les sessions MCP Streamable HTTP.

    Args:
        resource_id: ID de l'espace à vérifier

    Returns:
        None si OK, dict {"status": "error", ...} si refusé
    """
    # Ambient path: read the freshest token info from the contextvar
    # store and delegate to the shared evaluator. This keeps the
    # legacy behaviour byte-for-byte unchanged.
    return _evaluate_access(_get_effective_token_info(), resource_id)


def check_write_permission() -> Optional[dict]:
    """
    Vérifie que le token courant a la permission d'écriture.

    Hiérarchie : admin ⊃ manage ⊃ write → tous acceptés.

    Nécessaire pour : live_note, bank_consolidate, space_update,
    backup_create, graph_*.

    Returns:
        None si OK, dict {"status": "error", ...} si refusé
    """
    token_info = _get_effective_token_info()

    if token_info is None:
        return {"status": "error", "message": "Authentification requise"}

    permissions = token_info.get("permissions", [])
    if "write" in permissions or "manage" in permissions or "admin" in permissions:
        return None

    return {
        "status": "error",
        "message": "Permission 'write' requise pour cette opération",
    }


def check_manage_permission() -> Optional[dict]:
    """
    Vérifie que le token courant a la permission de gestion (manage).

    Hiérarchie : admin ⊃ manage → les deux acceptés.

    Nécessaire pour : space_create, token_create, space_invite_token,
    bank_write, bank_delete, bank_repair, bank_compact, space_delete,
    space_update_rules, backup_restore, backup_delete.

    Returns:
        None si OK, dict {"status": "error", ...} si refusé
    """
    token_info = _get_effective_token_info()

    if token_info is None:
        return {"status": "error", "message": "Authentification requise"}

    permissions = token_info.get("permissions", [])
    if "manage" in permissions or "admin" in permissions:
        return None

    return {
        "status": "error",
        "message": "Permission 'manage' requise pour cette opération",
    }


def check_admin_permission() -> Optional[dict]:
    """
    Vérifie que le token courant a la permission admin.

    Nécessaire pour : admin_audit_recent, admin_create_token,
    admin_list_tokens, admin_revoke_token, admin_update_token,
    admin_gc_notes.

    Returns:
        None si OK, dict {"status": "error", ...} si refusé
    """
    token_info = _get_effective_token_info()

    if token_info is None:
        return {"status": "error", "message": "Authentification requise"}

    permissions = token_info.get("permissions", [])
    if "admin" in permissions:
        return None

    return {
        "status": "error",
        "message": "Permission 'admin' requise pour cette opération",
    }


def safe_error(exception: Exception, context: str = "") -> dict:
    """
    VULN-27 fix : retourne un message d'erreur sécurisé.

    En mode debug (MCP_SERVER_DEBUG=true), retourne le message complet.
    En mode production, retourne un message générique et log les détails.

    Args:
        exception: L'exception capturée
        context: Contexte optionnel (nom de l'outil, ex: "live_note")

    Returns:
        {"status": "error", "message": "..."}
    """
    import logging
    from ..config import get_settings

    logger = logging.getLogger("live_mem.tools")
    logger.exception("Erreur dans %s: %s", context or "outil MCP", exception)

    if get_settings().mcp_server_debug:
        return {"status": "error", "message": str(exception)}

    return {"status": "error", "message": "Erreur interne du serveur"}


# ─────────────────────────────────────────────────────────────
# PolicyProvider seam (ADR-0003 Option 3)
# ─────────────────────────────────────────────────────────────
# P6-6 (#92) — Lands the canonical seam named in ADR-0003
# Implementation Notes §1: a narrow ``PolicyProvider`` Protocol with
# ``authorize(identity, action, resource, context)`` and a concrete
# ``MonoTenantSpaceAllowlistProvider`` default that fail-closes on any
# tenancy-shaped context. The default reuses ``check_access()`` for the
# space-allowlist + admin-bypass path so legitimate access is preserved
# byte-for-byte.
#
# A downstream edition (Lesur AI Portal) plugs in a tenant-aware
# subclass via the ``default_policy_provider()`` factory injection
# point; the seam is the public-repo surface, the tenancy logic lives
# in the closed-source edition (ADR-0003).


class PermissionDenied(Exception):
    """
    Raised by ``PolicyProvider.authorize`` when the policy denies an
    action. Default-deny is the fail-closed posture of the seam.

    Carries a single short message string; callers translate to their
    transport's deny shape (e.g. MCP tool error dict, HTTP 403).
    """


# Tenancy-shaped context keys. If any of these appear in the
# ``context`` dict passed to ``authorize`` with a non-None non-empty
# value, the default mono-tenant provider raises ``PermissionDenied``.
# Listing them explicitly makes the deny set part of the public seam
# contract (ADR-0003).
#
# NOTE: P6-6 (R2) hardens authorize() to fail-close on ANY unrecognized
# context key (see ``MonoTenantSpaceAllowlistProvider.RECOGNIZED_CONTEXT_KEYS``).
# This frozenset is retained as the explicit, named deny set for the
# tenancy shape so contributors see exactly which keys are tenancy-
# flavoured; the broader "deny any unknown key" check is the primary
# fail-closed gate, and this one is the belt-and-suspenders defence
# in case a downstream subclass widens ``RECOGNIZED_CONTEXT_KEYS``.
_TENANCY_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "tenant_id",
        "tenant",
        "organization_id",
        "organization",
        "workspace_id",
    }
)


@runtime_checkable
class PolicyProvider(Protocol):
    """
    Authorization seam reserved by ADR-0003 (Option 3).

    The OSS edition ships exactly one concrete implementation,
    :class:`MonoTenantSpaceAllowlistProvider`, which is also the
    factory default returned by :func:`default_policy_provider`. A
    downstream edition (Lesur AI Portal) may inject a subclass that
    layers tenant-aware policy on top; the OSS repo does not import
    or call any Portal namespace.

    Contract:
      - ``authorize`` returns ``None`` to allow.
      - ``authorize`` raises :class:`PermissionDenied` to deny.
      - Default posture is fail-closed: the implementation must deny
        any context shape it does not understand.

    See ``docs/EXTENSION_POINTS.md`` §2a for the public-promise wording
    and the Portal extension caveat.
    """

    def authorize(
        self,
        identity: Optional[dict],
        action: str,
        resource: str,
        context: Optional[dict] = None,
    ) -> None:
        """
        Authorize ``identity`` to perform ``action`` on ``resource``.

        Args:
            identity: Token info dict (same shape as
                ``current_token_info``) or ``None`` for unauthenticated.
            action: MCP-tool name or logical action label (free-form).
            resource: Space id (or other resource identifier).
            context: Optional structured context. The default provider
                fail-closes on any tenancy-shaped key.

        Returns:
            ``None`` on allow.

        Raises:
            PermissionDenied: on deny.
        """
        ...


class MonoTenantSpaceAllowlistProvider:
    """
    Default OSS :class:`PolicyProvider`.

    Fail-closes per ADR-0003 §Implementation Notes §1 on:

    1. missing or empty ``identity`` (no ambient fallback);
    2. missing ``action`` or any action outside the V1 closed set
       :attr:`ALLOWED_ACTIONS`;
    3. any ``context`` key outside the V1 recognized set
       :attr:`RECOGNIZED_CONTEXT_KEYS` (a stricter superset of the
       legacy tenancy-key deny);
    4. legacy belt-and-suspenders tenancy deny on
       ``_TENANCY_CONTEXT_KEYS`` (subsumed by (3) for the default
       config; preserved so a downstream subclass that widens
       ``RECOGNIZED_CONTEXT_KEYS`` still cannot accidentally let a
       tenancy claim through).

    Otherwise the supplied ``identity`` is evaluated against the
    per-space allowlist + admin-bypass logic via the shared
    :func:`_evaluate_access` helper. The supplied identity is
    **authoritative** — the seam never reads from the ambient
    ``current_token_info`` contextvar (ADR-0003 §Implementation Notes
    §1: "missing identity → deny").

    A downstream edition wires its own tenant-aware subclass via
    :func:`default_policy_provider` injection; the seam shape (this
    class's public signature) is the public-repo contract.
    """

    #: V1 closed set of MCP-tool actions the OSS edition recognizes.
    #: Derived from ``tests/fixtures/tool_surface.json`` (the
    #: surface-stability gate from P6-3). The list is enumerated
    #: explicitly so this module has no import-time file I/O and the
    #: deny is robust against fixture corruption.
    #:
    #: ADR-0003 §Implementation Notes §1: "unknown action → deny".
    #: A downstream Portal subclass that exposes new tool names MUST
    #: extend this set explicitly (e.g. via ``ALLOWED_ACTIONS |
    #: {"portal_extra"}``) — extending implicitly is not supported.
    ALLOWED_ACTIONS: frozenset[str] = frozenset(
        {
            # ---- admin tools ----
            "admin_audit_recent",
            "admin_bulk_update_tokens",
            "admin_create_token",
            "admin_delete_token",
            "admin_gc_notes",
            "admin_list_tokens",
            "admin_purge_tokens",
            "admin_revoke_token",
            "admin_update_token",
            # ---- backup tools ----
            "backup_create",
            "backup_delete",
            "backup_download",
            "backup_list",
            "backup_restore",
            # ---- bank (mid) tools — historical aliases ----
            "bank_compact",
            "bank_consolidate",
            "bank_consolidation_queues",
            "bank_consolidation_status",
            "bank_delete",
            "bank_list",
            "bank_read",
            "bank_read_all",
            "bank_repair",
            "bank_stale_spaces",
            "bank_write",
            # ---- graph (long) tools — historical aliases ----
            "graph_connect",
            "graph_disconnect",
            "graph_push",
            "graph_status",
            # ---- live (short) tools — historical aliases ----
            "live_note",
            "live_read",
            "live_search",
            # ---- long tools (no alias mapping) ----
            "long_ingest",
            "long_query",
            # ---- space tools ----
            "space_create",
            "space_delete",
            "space_export",
            "space_info",
            "space_invite_token",
            "space_list",
            "space_rules",
            "space_summary",
            "space_update",
            "space_update_rules",
            # ---- system tools ----
            "system_about",
            "system_health",
            "system_whoami",
            # ---- delegated access management ----
            "token_create",
            # ---- canonical tier aliases (short_*/mid_*/long_*) ----
            "short_note",
            "short_read",
            "short_search",
            "mid_consolidate",
            "mid_delete",
            "mid_list",
            "mid_read",
            "mid_read_all",
            "mid_write",
            "long_connect",
            "long_disconnect",
            "long_push",
            "long_status",
        }
    )

    #: V1 recognized set of ``context`` keys. The OSS mono-tenant
    #: provider does not consume any structured context, so this
    #: set is **empty** by design. Any key seen in ``context`` is
    #: therefore unrecognized and denied.
    #:
    #: ADR-0003 §Implementation Notes §1: "unparseable policy context
    #: → deny". A downstream Portal subclass that wants to evaluate
    #: e.g. ``tenant_id`` MUST extend this set explicitly and pair the
    #: extension with the corresponding evaluation logic — the OSS
    #: default treats any unknown key as a deny.
    RECOGNIZED_CONTEXT_KEYS: frozenset[str] = frozenset()

    @staticmethod
    def _identity_is_empty(identity: Optional[dict]) -> bool:
        """
        An identity is "empty" if it is ``None``, an empty dict, or
        has no usable claim fields (no ``client_name`` and no
        ``permissions`` and no ``allowed_resources``). The fail-closed
        posture treats such an identity exactly as missing.
        """
        if identity is None:
            return True
        if not isinstance(identity, dict):
            return True
        if not identity:
            return True
        # A TokenInfo-shaped dict with no usable claims is functionally
        # missing — deny per ADR-0003.
        if (
            not identity.get("client_name")
            and not identity.get("permissions")
            and identity.get("allowed_resources") is None
        ):
            return True
        return False

    def authorize(
        self,
        identity: Optional[dict],
        action: str,
        resource: str,
        context: Optional[dict] = None,
    ) -> None:
        # ---- (1) Fail-closed on missing or empty identity ----
        # ADR-0003 §Implementation Notes §1: "missing identity → deny".
        # No fallback to the ambient ``current_token_info`` here — the
        # seam's contract is that the SUPPLIED identity is
        # authoritative.
        if self._identity_is_empty(identity):
            raise PermissionDenied(
                "OSS mono-tenant: missing or empty identity"
            )

        # ---- (2) Fail-closed on missing action ----
        if not action:
            raise PermissionDenied("OSS mono-tenant: missing action")

        # ---- (3) Fail-closed on unknown action ----
        # ADR-0003 §Implementation Notes §1: "unknown action → deny".
        if action not in self.ALLOWED_ACTIONS:
            raise PermissionDenied(
                f"OSS mono-tenant: unknown action {action!r}"
            )

        # ---- (3b) Fail-closed on malformed context shape ----
        # ADR-0003 §Implementation Notes §1: "unparseable policy
        # context → deny". The public seam accepts a dynamic Python
        # context argument; the contract is that it MUST be either
        # ``None`` (no context) or a ``dict``. Any other type — falsy
        # (``[]``, ``""``, ``0``, ``False``) or truthy (a list, str,
        # int, set, custom object, ...) — is malformed and must fail
        # closed BEFORE we attempt to iterate keys (which would either
        # silently treat falsy non-dicts as "no context" or crash with
        # AttributeError on truthy non-dicts).
        if context is not None and not isinstance(context, dict):
            raise PermissionDenied(
                "OSS mono-tenant: context must be a dict or None, "
                f"got {type(context).__name__}"
            )

        # ---- (4) Fail-closed on unrecognized context key ----
        # ADR-0003 §Implementation Notes §1: "unparseable policy
        # context → deny". Any key the V1 implementation does not
        # know how to evaluate triggers a deny — broader than the
        # legacy 5-tenancy-key deny in (5) below.
        if context:
            unknown_keys = [
                k for k in context.keys()
                if k not in self.RECOGNIZED_CONTEXT_KEYS
            ]
            if unknown_keys:
                raise PermissionDenied(
                    "OSS mono-tenant: unrecognized context key "
                    f"{unknown_keys[0]!r}"
                )

        # ---- (5) Belt-and-suspenders tenancy deny ----
        # Subsumed by (4) for the default config (where
        # ``RECOGNIZED_CONTEXT_KEYS`` is empty), but kept here so that
        # a downstream subclass widening ``RECOGNIZED_CONTEXT_KEYS``
        # cannot accidentally let a tenancy claim through without
        # re-implementing the tenancy gate.
        if context:
            for key in _TENANCY_CONTEXT_KEYS:
                value = context.get(key)
                if value:
                    raise PermissionDenied(
                        "OSS mono-tenant: unsupported tenancy context"
                    )

        # ---- (6) Space-allowlist + admin-bypass on SUPPLIED identity ----
        # We delegate to the shared ``_evaluate_access`` helper using
        # the explicit ``identity`` — NOT the ambient
        # ``current_token_info`` contextvar. This is the seam's
        # authoritative-identity guarantee (ADR-0003 §Implementation
        # Notes §1) and ensures the explicit-identity path cannot
        # silently fall back to ambient state.
        err = _evaluate_access(identity, resource)
        if err is not None:
            raise PermissionDenied(err.get("message", "Accès refusé"))


# Module-level singleton. A downstream edition that wants to inject a
# different default does so by reassigning this name in its own
# bootstrap (the OSS repo never reads from a Portal namespace).
_DEFAULT_POLICY_PROVIDER: PolicyProvider = MonoTenantSpaceAllowlistProvider()


def default_policy_provider() -> PolicyProvider:
    """
    Return the process-wide default :class:`PolicyProvider`.

    OSS default is :class:`MonoTenantSpaceAllowlistProvider`. A
    downstream edition (Lesur AI Portal) may replace the singleton in
    its own bootstrap; the public-repo contract is that this factory
    is the only seam call-sites are allowed to read from.
    """
    return _DEFAULT_POLICY_PROVIDER


def get_current_agent_name() -> str:
    """
    Retourne le nom de l'agent (client_name du token courant).

    Utile pour identifier automatiquement l'auteur d'une note live
    quand le paramètre agent n'est pas fourni.

    Returns:
        Nom de l'agent, ou "anonymous" si pas de token
    """
    # Use the same request-aware identity path as the permission guards.  MCP
    # Streamable HTTP session tasks can retain a stale contextvar from session
    # initialization; default-own operations must follow the bearer identity
    # attached to the current request, never that stale session snapshot.
    token_info = get_effective_token_info()
    if token_info is None:
        return "anonymous"
    return token_info.get("client_name", "anonymous")
