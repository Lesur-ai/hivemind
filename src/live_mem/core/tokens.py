# -*- coding: utf-8 -*-
"""
Service Tokens — Gestion des tokens d'authentification.

Les tokens sont stockés dans _system/tokens.json sur S3.
Chaque token est hashé en SHA-256 avant stockage (jamais en clair).

Architecture :
    tools/admin.py → TokenService (ce fichier) → StorageService (S3)
    auth/middleware.py → TokenService.validate_token()

Concurrence :
    Protégé par asyncio.Lock (via LockManager.tokens) pour les
    opérations read-modify-write sur tokens.json.

Voir AUTH_AND_COLLABORATION.md pour le modèle complet.
"""

import json
import logging
import secrets
import hashlib
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from .storage import get_storage
from .locks import get_lock_manager
from .models import TokenInfo, TokensStore, INTERNAL_LONG_TOKEN_NAME

# Logger dédié pour les événements d'audit (consommé par AuditMiddleware
# en parallèle ; voir middleware.py). Émettre ici garantit une trace même
# si le retour MCP est perdu côté client (review PR #14, point #4).
_audit_logger = logging.getLogger("live_mem.audit")


# Préfixe des tokens générés
TOKEN_PREFIX = "lm_"

# Chemin S3 du registre de tokens
TOKENS_KEY = "_system/tokens.json"

# v2 rend la migration ``space_ids=[]`` one-shot. Un store v1 est lisible
# uniquement par ``migrate_empty_space_ids`` au démarrage ; tout autre chemin
# d'autorisation échoue fermé tant que la migration n'est pas durable.
CURRENT_TOKENS_VERSION = 2
LEGACY_TOKENS_VERSION = 1

# Permissions reconnues par le système d'authentification
# Hiérarchie inclusive : admin ⊃ manage ⊃ write ⊃ read
VALID_PERMISSIONS = {"read", "write", "manage", "admin"}

# LM2-11 — surface déléguée manage. Contrairement à ``create_token``
# (compatibilité admin historique), ces profils sont fermés et ordonnés : un
# manager peut déléguer jusqu'à ``manage``, jamais ``admin``.
DELEGATED_PERMISSION_PROFILES: dict[str, list[str]] = {
    "read": ["read"],
    "read,write": ["read", "write"],
    "read,write,manage": ["read", "write", "manage"],
}

_CANONICAL_TOKEN_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CANONICAL_SPACE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
_TARGET_NOT_INVITABLE = {
    "status": "error",
    "message": "Target token cannot be invited",
}


def _expiry_days_validation_error(expires_in_days: object) -> str | None:
    """Valide une durée avant toute génération de secret.

    ``timedelta`` accepte une plage bien plus large que ``datetime``. Tester
    aussi l'addition ferme donc le cas où un entier gigantesque lèverait après
    création du plaintext, et conserve le contrat ``0 = jamais`` sans accepter
    silencieusement les valeurs négatives.
    """
    if not isinstance(expires_in_days, int) or isinstance(expires_in_days, bool):
        return "expires_in_days must be an integer"
    if expires_in_days < 0:
        return "expires_in_days must be >= 0"
    try:
        datetime.now(timezone.utc) + timedelta(days=expires_in_days)
    except (OverflowError, TypeError):
        return "expires_in_days hors plage supportée"
    return None


def _token_is_expired(expires_at: str, now_dt: datetime) -> bool:
    """HM-20 : ``expires_at`` (ISO 8601) est-il ≤ ``now_dt`` ?

    Comparaison en ``datetime`` (pas lexicographique sur str). Fail-closed :
    un ``expires_at`` illisible est traité comme EXPIRÉ (True → token refusé).
    """
    try:
        exp = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True  # malformé → expiré (fail-closed)
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    return exp <= now_dt


def _authorization_profile_changed(
    before_permissions: list[str],
    after_permissions: list[str],
    before_space_ids: list[str],
    after_space_ids: list[str],
) -> bool:
    """Compare effective unordered permission/scope state for reconnect hints.

    Permission order and allowlist order do not affect authorization. Duplicate
    permission validity does affect fail-closed discovery, so that bit remains
    part of the comparison even when the unique permission set is unchanged.
    """

    before_permission_state = (
        frozenset(before_permissions),
        len(before_permissions) == len(set(before_permissions)),
    )
    after_permission_state = (
        frozenset(after_permissions),
        len(after_permissions) == len(set(after_permissions)),
    )
    return (
        before_permission_state != after_permission_state
        or set(before_space_ids) != set(after_space_ids)
    )


class TokenService:
    """
    Service de gestion des tokens d'authentification.

    Toutes les opérations de modification (create, revoke, update)
    sont protégées par un asyncio.Lock pour éviter les conflits.
    """

    def __init__(self):
        # VULN-01 fix : cache en mémoire pour last_used_at
        # Évite la race condition d'écriture S3 dans validate_token()
        self._last_used_cache: dict[str, str] = {}

    def _find_token_by_hash(self, store: "TokensStore", token_hash: str) -> tuple:
        """
        Trouve un token par préfixe de hash (VULN-03 fix).

        Retourne (index, token) ou (-1, None) si introuvable.
        Retourne (-2, None) si le préfixe est ambigu (multiple matches).

        Exige un minimum de 16 caractères pour le préfixe (hex pur ou avec
        préfixe ``sha256:``). Le préfixe ``sha256:`` est optionnel — il est
        accepté tel quel ou ajouté implicitement si l'utilisateur fournit
        uniquement le hex (issue #11).
        """
        # Issue #11 fix : normaliser l'entrée pour accepter les deux formes
        # ("sha256:abc..." comme retourné par admin_list_tokens, ou juste "abc..."
        # qui est ce que les utilisateurs copient naturellement).
        normalized = (
            token_hash if token_hash.startswith("sha256:") else "sha256:" + token_hash
        )

        # La validation min 16 chars s'applique sur le hex pur (8 octets de hash),
        # pas sur la longueur incluant le préfixe.
        hex_only = normalized[len("sha256:"):]
        if len(hex_only) < 16:
            return (-3, None)  # Préfixe trop court

        matches = [
            (i, t) for i, t in enumerate(store.tokens) if t.hash.startswith(normalized)
        ]

        if len(matches) == 0:
            return (-1, None)
        if len(matches) > 1:
            return (-2, None)
        return matches[0]

    def _token_not_found_or_ambiguous(self, idx: int, token_hash: str) -> dict | None:
        """Retourne un message d'erreur si le token n'est pas trouvé, ou None si OK."""
        if idx == -3:
            # Review #12 fix : afficher la longueur du hex pur (pas du préfixé),
            # car la validation min 16 chars s'applique sur le hex.
            hex_part = (
                token_hash[len("sha256:") :]
                if token_hash.startswith("sha256:")
                else token_hash
            )
            return {
                "status": "error",
                "message": (
                    f"Hash hex trop court ({len(hex_part)} chars). "
                    "Minimum 16 caractères hex requis."
                ),
            }
        if idx == -2:
            return {
                "status": "error",
                "message": "Ambiguous hash prefix: multiple tokens match. Provide a longer hash.",
            }
        if idx == -1:
            return {"status": "not_found", "message": "Token not found"}
        return None

    async def _resolve_space_ids(self, space_ids: str) -> tuple[list[str], bool]:
        """
        Résout l'argument ``space_ids`` en liste matérialisée.

        Gère le sucre syntaxique ``"*"`` / ``"all"`` (snapshot des espaces
        existants au moment de l'appel) — partagé entre ``create_token`` et
        ``update_token`` pour garantir une UX cohérente (review #12).

        Args:
            space_ids: Chaîne d'entrée. Une liste séparée par virgules,
                ou ``"*"``/``"all"`` (snapshot), ou vide.

        Returns:
            Tuple ``(sid_list, snapshot_used)`` :

            - ``sid_list`` : liste matérialisée des space_ids
            - ``snapshot_used`` : ``True`` si le sucre ``*``/``all`` a été
              utilisé (la réponse appelante ajoutera des champs informatifs).
        """
        space_ids_stripped = (space_ids or "").strip()
        if space_ids_stripped.lower() in ("*", "all"):
            from .space import get_space_service  # import local pour éviter cycles

            spaces_result = await get_space_service().list_spaces()
            if spaces_result.get("status") == "ok":
                return (
                    [s["space_id"] for s in spaces_result.get("spaces", [])],
                    True,
                )
            return ([], True)
        return ([s.strip() for s in space_ids.split(",") if s.strip()], False)

    @staticmethod
    def _invalidate_in_fresh_store(token_hashes: list[str]) -> None:
        """
        LM2-07 fix : invalide une liste de tokens dans le store global.

        Délégation à ``auth.context.invalidate_token_in_store`` pour
        chaque hash. Import local pour éviter un cycle (auth importe
        core indirectement).

        Idempotent et best-effort : un échec silencieux ne casse pas
        la mutation S3 déjà persistée.
        """
        try:
            from ..auth.context import invalidate_token_in_store
        except Exception:
            return
        for h in token_hashes:
            if not h:
                continue
            try:
                invalidate_token_in_store(h)
            except Exception:
                # Logging best-effort, on n'interrompt pas le flux mutateur
                pass

    @staticmethod
    def _muted_token_warning() -> str:
        """Message standard pour les tokens "muets" (issue #11)."""
        return (
            "⚠️ Ce token n'a accès à aucun espace existant (space_ids=[]). "
            "Depuis v1.5.0, c'est la sémantique stricte par défaut. "
            "Utilisez space_ids='*' pour un snapshot de tous les espaces "
            "actuels, ou listez-les explicitement (ex: 'space-a,space-b'). "
            "Un token manage autorisé peut ensuite l'inviter avec "
            "space_invite_token."
        )

    @staticmethod
    def _find_exact_token(store: TokensStore, token_hash: str) -> Optional[TokenInfo]:
        """Retourne uniquement une correspondance de hash canonique complet."""
        if not _CANONICAL_TOKEN_HASH_RE.fullmatch(token_hash or ""):
            return None
        return next((t for t in store.tokens if t.hash == token_hash), None)

    @staticmethod
    def _is_active(token: Optional[TokenInfo], now_dt: datetime) -> bool:
        """Un token stocké est actif seulement s'il est présent, non révoqué et frais."""
        if token is None or token.revoked:
            return False
        return not (
            token.expires_at and _token_is_expired(token.expires_at, now_dt)
        )

    def _authorize_stored_manager(
        self,
        store: TokensStore,
        actor_token_hash: str,
        *,
        space_id: str | None = None,
        now_dt: datetime | None = None,
    ) -> Optional[TokenInfo]:
        """Ré-authentifie un acteur manage/admin depuis le store tenu verrouillé.

        Le contextvar MCP n'est qu'un indice de requête ; il ne constitue pas
        l'autorité de mutation. Le caller fournit donc son hash exact, puis ce
        helper relit l'entrée S3 sous ``LockManager.tokens``. Un bootstrap key
        n'a pas de hash stocké et est refusé par construction.

        Pour une invitation, ``space_id`` impose aussi le scope persistant du
        manager. Les admins stockés conservent leur bypass global.
        """
        now_dt = now_dt or datetime.now(timezone.utc)
        actor = self._find_exact_token(store, actor_token_hash)
        if not self._is_active(actor, now_dt):
            return None
        assert actor is not None  # affiné par _is_active
        permissions = set(actor.permissions or [])
        if not ({"manage", "admin"} & permissions):
            return None
        if space_id is not None and "admin" not in permissions:
            if space_id not in actor.space_ids:
                return None
        return actor

    @staticmethod
    def _emit_delegated_access_audit(
        event: str,
        *,
        caller: str,
        details: dict,
    ) -> None:
        """Émet un audit minimal après persistance confirmée.

        Les hashes canoniques complets servent d'identifiants d'audit ; le
        secret plaintext n'est jamais inclus. Le logger reste best-effort car
        l'état durable est déjà écrit.
        """
        try:
            from ..middleware import current_request_id

            request_id = current_request_id.get()
        except Exception:
            request_id = "-"
        entry = {
            "event": event,
            "request_id": request_id,
            "caller": caller,
            **details,
        }
        try:
            _audit_logger.info(json.dumps(entry, ensure_ascii=False))
        except Exception:
            pass

    async def create_delegated_token(
        self,
        *,
        actor_token_hash: str,
        name: str,
        permissions: str,
        expires_in_days: int = 0,
        email: str = "",
    ) -> dict:
        """Crée un token sans scope initial au nom d'un manage/admin S3.

        LM2-11 sépare le provisionnement de l'écriture courante : le caller est
        revalidé depuis ``_system/tokens.json`` sous verrou, les seuls profils
        délégables sont ``read``, ``read,write`` et
        ``read,write,manage``, et le secret n'est généré qu'après toutes les
        validations. Le bootstrap key et le nom interne réservé sont refusés.
        """
        clean_name = (name or "").strip()
        if not clean_name:
            return {"status": "error", "message": "Token name is required"}
        if clean_name == INTERNAL_LONG_TOKEN_NAME:
            return {"status": "error", "message": "Reserved token name"}

        # Frontière fermée jusque dans le service : FastMCP impose déjà
        # le Literal, mais aucun appel interne ne doit pouvoir contourner la
        # grammaire canonique via espaces, réordonnancement ou doublons.
        perm_list = DELEGATED_PERMISSION_PROFILES.get(permissions)
        if perm_list is None:
            return {
                "status": "error",
                "message": (
                    "Profil de permissions invalide. Valeurs acceptées : "
                    "read | read,write | read,write,manage"
                ),
            }
        expiry_error = _expiry_days_validation_error(expires_in_days)
        if expiry_error:
            return {"status": "error", "message": expiry_error}

        async with get_lock_manager().tokens:
            store = await self._load_store()
            # Prendre l'heure APRÈS l'attente du verrou : un manager qui expire
            # pendant cette attente ne doit pas conserver une autorité périmée.
            now_dt = datetime.now(timezone.utc)
            actor = self._authorize_stored_manager(
                store, actor_token_hash, now_dt=now_dt
            )
            if actor is None:
                return {
                    "status": "error",
                    "message": "An active S3 manage or admin token is required",
                }

            # Le plaintext est produit seulement après validation du caller et
            # du payload. Défense collisionnelle explicite malgré l'entropie.
            while True:
                raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
                token_hash = "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()
                if all(t.hash != token_hash for t in store.tokens):
                    break

            expires_at = None
            if expires_in_days > 0:
                expires_at = (now_dt + timedelta(days=expires_in_days)).isoformat()
            token_info = TokenInfo(
                hash=token_hash,
                name=clean_name,
                email=email,
                permissions=list(perm_list),
                space_ids=[],
                created_at=now_dt.isoformat(),
                expires_at=expires_at,
            )
            store.tokens.append(token_info)

            try:
                await self._save_store(store)
            except Exception:
                # Un timeout S3 peut survenir après le PUT. Reprobe sous le
                # même verrou. Si la lecture est elle-même ambiguë, retourner
                # le secret en ``partial`` est le seul chemin never-orphan.
                try:
                    persisted = await self._load_store()
                except Exception:
                    # Never-orphan : la persistance est ambiguë et aucun admin
                    # ne pourrait retrouver le plaintext depuis S3. On rend le
                    # credential au caller avec un statut explicitement non
                    # final afin qu'un admin vérifie le hash exact avant retry.
                    return {
                        "status": "partial",
                        "recovery_required": True,
                        "token": raw_token,
                        "token_hash": token_hash,
                        "message": (
                            "Persistance du token ambiguë. Conservez le secret et "
                            "faites vérifier ce token_hash exact par un admin avant "
                            "toute nouvelle création."
                        ),
                    }
                confirmed = self._find_exact_token(persisted, token_hash)
                if confirmed is None:
                    return {
                        "status": "error",
                        "message": "Creation was not persisted; no token was created",
                    }
                if confirmed.model_dump() != token_info.model_dump():
                    return {
                        "status": "partial",
                        "recovery_required": True,
                        "token": raw_token,
                        "token_hash": token_hash,
                        "message": (
                            "Un enregistrement conflictuel existe pour le hash généré. "
                            "Conservez le secret et faites inspecter le hash exact par "
                            "un admin ; ne retentez pas automatiquement."
                        ),
                    }

        self._emit_delegated_access_audit(
            "token_create",
            caller=actor.name,
            details={
                "actor_token_hash": actor.hash,
                "name": clean_name,
                "permissions": list(perm_list),
                "token_hash": token_hash,
            },
        )
        return {
            "status": "created",
            "name": clean_name,
            "token": raw_token,
            "token_hash": token_hash,
            "permissions": list(perm_list),
            "space_ids": [],
            "expires_at": expires_at,
            "warning": "Save this token now; it will not be shown again.",
            "warning_no_access": (
                "Ce token n'a accès à aucun espace. Un manager autorisé doit "
                "l'inviter avec space_invite_token."
            ),
        }

    async def invite_token_to_space(
        self,
        *,
        actor_token_hash: str,
        space_id: str,
        target_token_hash: str,
    ) -> dict:
        """Ajoute idempotemment un token actif à un space autorisé au caller.

        Le hash cible doit être le hash canonique complet. Toutes les cibles
        non invitables (forme invalide, inconnue, révoquée, expirée, admin ou
        ``internal-long``) partagent la même erreur opaque.
        """
        from .space import SPACE_ID_REGEX, SpaceService

        if not SPACE_ID_REGEX.fullmatch(space_id or ""):
            return {"status": "error", "message": "Invalid space identifier"}
        if not _CANONICAL_TOKEN_HASH_RE.fullmatch(target_token_hash or ""):
            return dict(_TARGET_NOT_INVITABLE)

        storage = get_storage()
        added = False
        actor_name = "unknown"
        locks = get_lock_manager()
        async with locks.space_lifecycle(space_id):
            async with locks.tokens:
                store = await self._load_store()
                now_dt = datetime.now(timezone.utc)
                actor = self._authorize_stored_manager(
                    store,
                    actor_token_hash,
                    space_id=space_id,
                    now_dt=now_dt,
                )
                if actor is None:
                    return {
                        "status": "error",
                        "message": "Active manage access is required for this space",
                    }
                actor_name = actor.name

                # L'état committé COMPLET est vérifié APRÈS l'autorité du caller,
                # pour ne pas transformer cet outil en oracle d'existence. La
                # classification est partagée avec space_create afin que marker
                # corrompu/partiel ne puisse jamais recevoir de grant.
                space_state, _reason = await SpaceService.classify_committed_state(
                    storage, space_id
                )
                if space_state == "absent":
                    return {"status": "not_found", "message": "Space not found"}
                if space_state != "committed":
                    return {
                        "status": "error",
                        "message": "Space unavailable: recovery required",
                        "recovery_required": True,
                    }

                target = self._find_exact_token(store, target_token_hash)
                if (
                    not self._is_active(target, now_dt)
                    or target is None
                    or "admin" in set(target.permissions or [])
                    or target.name == INTERNAL_LONG_TOKEN_NAME
                ):
                    return dict(_TARGET_NOT_INVITABLE)

                if space_id not in target.space_ids:
                    target.space_ids.append(space_id)
                    try:
                        await self._save_store(store)
                    except Exception:
                        # Même traitement des timeouts post-PUT : confirmer la
                        # seule post-condition autorisée avant succès/cache/audit.
                        try:
                            persisted = await self._load_store()
                        except Exception:
                            return {
                                "status": "partial",
                                "recovery_required": True,
                                "message": (
                                    "Persistance de l'invitation ambiguë. Un admin "
                                    "doit vérifier le hash cible exact avant retry."
                                ),
                            }
                        confirmed = self._find_exact_token(
                            persisted, target_token_hash
                        )
                        if confirmed is None or space_id not in confirmed.space_ids:
                            return {
                                "status": "error",
                                "message": "Invitation was not persisted",
                            }
                    added = True

        if added:
            self._invalidate_in_fresh_store([target_token_hash])
        self._emit_delegated_access_audit(
            "space_invite_token",
            caller=actor_name,
            details={
                "actor_token_hash": actor_token_hash,
                "space_id": space_id,
                "target_token_hash": target_token_hash,
                "added": added,
            },
        )
        response = {"status": "ok", "space_id": space_id, "added": added}
        if added:
            # P10-1: call-time authorization is already fresh; this flag tells
            # clients that their cached tools/list projection may be stale.
            response["mcp_reconnect_required"] = True
        return response


    async def create_token(
        self,
        name: str,
        permissions: str,
        space_ids: str = "",
        expires_in_days: int = 0,
        email: str = "",
    ) -> dict:
        """
        Crée un nouveau token d'authentification.

        Le token en clair est retourné UNE SEULE FOIS. Seul le hash
        SHA-256 est stocké dans tokens.json.

        Args:
            name: Nom descriptif (ex: "agent-cline")
            permissions: Sous-ensemble CSV de read/write/manage/admin. Profils
                standards : "read", "read,write", "read,write,manage" ou
                "read,write,manage,admin".
            space_ids: Espaces autorisés séparés par virgules.
                Sémantique v1.5.0+ pour les non-admin :

                - ``""`` (vide) → **aucun accès** aux espaces existants
                  (un manager/admin devra l'inviter explicitement).
                - ``"a,b,c"`` → accès uniquement à ces espaces.
                - ``"*"`` ou ``"all"`` → snapshot des espaces existants au
                  moment de la création (pas les futurs spaces ; aligné
                  avec la sémantique stricte v1.5.0).

                Pour les tokens admin, ``space_ids`` est ignoré et persisté
                vide (accès global par permission, aucun scope dormant).
            expires_in_days: Durée en jours (0 = jamais)

        Returns:
            ``{"status": "created", "token": "lm_...", ...}``.
            Si le token résultant n'a accès à aucun espace existant et n'est
            pas admin, un champ ``warning_no_access`` explicite est ajouté à
            la réponse (issue #11). Un PUT ambigu est reprobed : confirmation
            exacte → ``created`` ; absence confirmée → ``error`` sans secret ;
            lecture ambiguë ou conflit → ``partial`` avec plaintext + hash
            pour respecter never-orphan.
        """
        # Valider l'expiration avant toute résolution S3, attente de lock ou
        # génération du secret. ``0`` est le seul sentinel "jamais".
        expiry_error = _expiry_days_validation_error(expires_in_days)
        if expiry_error:
            return {"status": "error", "message": expiry_error}

        # ``client_name`` is the durable agent identity and the empty string is
        # reserved on bank_consolidate's wire contract for explicit global
        # scope. Never mint a token that cannot be isolated as an agent.
        if not isinstance(name, str) or name == "":
            return {"status": "error", "message": "Token name is required"}

        # Parser et valider les permissions
        if not isinstance(permissions, str):
            return {"status": "error", "message": "Permissions are required"}
        perm_list = [p.strip() for p in permissions.split(",") if p.strip()]
        if not perm_list:
            return {"status": "error", "message": "Permissions are required"}
        invalid = [p for p in perm_list if p not in VALID_PERMISSIONS]
        if invalid:
            return {
                "status": "error",
                "message": (
                    f"Permissions invalides : {invalid}. "
                    f"Valeurs acceptées : {sorted(VALID_PERMISSIONS)}"
                ),
            }
        if len(set(perm_list)) != len(perm_list):
            return {
                "status": "error",
                "message": "Duplicate permissions are not allowed",
            }

        # Les scopes d'un admin sont ignorés à l'autorisation et ne doivent pas
        # rester dormants dans le registre : un downgrade ultérieur les rendrait
        # actifs sans invitation explicite. Le schéma v2 impose donc [] pour
        # tout profil admin. Les non-admin conservent le sucre snapshot.
        space_ids_stripped = (space_ids or "").strip()
        is_admin = "admin" in perm_list
        if is_admin:
            sid_list, snapshot_used = [], False
        else:
            sid_list, snapshot_used = await self._resolve_space_ids(space_ids)
        if len(set(sid_list)) != len(sid_list):
            return {
                "status": "error",
                "message": "Duplicate space_ids are not allowed",
            }
        invalid_space_ids = [
            sid for sid in sid_list if not _CANONICAL_SPACE_ID_RE.fullmatch(sid)
        ]
        if invalid_space_ids:
            return {
                "status": "error",
                "message": f"space_ids invalides : {invalid_space_ids}",
            }

        # Sauvegarder sous lock. Le secret n'est généré qu'après toutes les
        # validations, puis toute erreur de PUT est reprobed pour garantir le
        # contrat never-orphan du credential retourné une seule fois.
        async with get_lock_manager().tokens:
            store = await self._load_store()
            while True:
                raw_token = TOKEN_PREFIX + secrets.token_urlsafe(32)
                token_hash = "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()
                if all(t.hash != token_hash for t in store.tokens):
                    break

            now = datetime.now(timezone.utc)
            expires_at = None
            if expires_in_days > 0:
                expires_at = (now + timedelta(days=expires_in_days)).isoformat()
            token_info = TokenInfo(
                hash=token_hash,
                name=name,
                email=email,
                permissions=perm_list,
                space_ids=sid_list,
                created_at=now.isoformat(),
                expires_at=expires_at,
            )
            store.tokens.append(token_info)
            try:
                await self._save_store(store)
            except Exception:
                # Le PUT peut avoir persisté avant un timeout. Une lecture
                # concluante distingue succès et absence ; une lecture ambiguë
                # doit rendre le plaintext pour ne jamais orpheliner le secret.
                try:
                    persisted = await self._load_store()
                except Exception:
                    return {
                        "status": "partial",
                        "recovery_required": True,
                        "token": raw_token,
                        "token_hash": token_hash,
                        "message": (
                            "Persistance du token ambiguë. Conservez le secret et "
                            "faites vérifier ce token_hash exact avant tout retry."
                        ),
                    }
                confirmed = self._find_exact_token(persisted, token_hash)
                if confirmed is None:
                    return {
                        "status": "error",
                        "message": "Creation was not persisted; no token was created",
                    }
                if confirmed.model_dump() != token_info.model_dump():
                    return {
                        "status": "partial",
                        "recovery_required": True,
                        "token": raw_token,
                        "token_hash": token_hash,
                        "message": (
                            "Enregistrement conflictuel pour le hash généré. "
                            "Conservez le secret et demandez une inspection admin."
                        ),
                    }

        response = {
            "status": "created",
            "name": name,
            "token": raw_token,
            "token_hash": token_hash,
            "permissions": perm_list,
            "space_ids": sid_list,
            "expires_at": expires_at,
            "warning": "Save this token now; it will not be shown again.",
        }

        # Issue #11 fix : signaler explicitement les tokens "muets"
        # (non-admin avec aucun space autorisé) — ces tokens recevraient un 403
        # sur tout espace existant. Un manager/admin doit les inviter avec
        # space_invite_token ; seuls manage/admin peuvent créer un espace.
        if not is_admin and not sid_list:
            response["warning_no_access"] = self._muted_token_warning()

        if is_admin and space_ids_stripped:
            response["scope_normalized"] = True
            response["info"] = (
                "Un token admin a un accès global par permission : space_ids "
                "a été ignoré et stocké vide pour empêcher tout scope dormant "
                "lors d'un downgrade ultérieur."
            )

        if snapshot_used:
            response["snapshot_taken"] = True
            response["info"] = (
                f"space_ids='{space_ids_stripped}' interprété comme snapshot "
                f"des {len(sid_list)} espace(s) existant(s) au moment de la création. "
                "Les futurs nouveaux espaces ne seront PAS automatiquement ajoutés."
            )

        return response

    async def register_internal_long_token(
        self,
        raw_token: str,
        *,
        name: str = INTERNAL_LONG_TOKEN_NAME,
        permissions: Optional[list[str]] = None,
    ) -> dict:
        """P7-3 — Enregistre (idempotent) le hash du token interne long.

        Contrairement à ``create_token`` (qui GÉNÈRE son propre plaintext), on
        enregistre ici le hash d'un plaintext DÉJÀ résolu (env / volume local,
        cf. ``core/embedded_secret.py``), afin que le GM embarqué le valide via
        le store S3 ``_system/tokens.json`` (Model B, P7-4) — révocation
        immédiate, jamais le raccourci bootstrap admin.

        Rotation (P7-3 R5) : garantit EXACTEMENT UN token actif portant le nom
        RÉSERVÉ ``name``. Toute AUTRE entrée de ce nom dont le hash diffère est
        révoquée — JAMAIS un token opérateur (scope strict au nom réservé,
        never-orphan). Ainsi une perte de volume + régénération invalide
        l'ancien hash au lieu de laisser deux tokens internes actifs.

        Idempotent : ré-appeler avec le même plaintext est un no-op (aucune
        écriture). Fail-closed : si l'entrée du hash courant est déjà révoquée
        (révocation opérateur explicite), elle N'EST PAS ré-activée — le bind
        échouera fermé.

        Least-privilege (P7-8) : le token interne porte EXACTEMENT
        ``{"read","write"}`` — jamais ``manage``, jamais ``admin``, jamais un
        sous-ensemble. Tout autre set est REJETÉ fail-closed : ce seam est le
        SEUL enregistreur du credential interne, et un scope élargi ouvrirait
        les surfaces destructives/admin de GM (backup_restore GM-native,
        admin_*) au runtime embarqué. Le GM force ``memory_ids=[]``
        (mono-tenant, P7-4).
        """
        if not raw_token:
            return {"status": "error", "message": "raw_token is required"}

        perms = list(permissions) if permissions else ["read", "write"]
        if set(perms) != {"read", "write"}:
            return {
                "status": "error",
                "message": (
                    "Le token interne long porte EXACTEMENT {'read','write'} "
                    f"(P7-8, least-privilege) ; reçu : {sorted(set(perms))}"
                ),
            }

        token_hash = "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()
        async with get_lock_manager().tokens:
            store = await self._load_store()
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat()

            # Operator revocation and expiry are terminal for the exact local
            # plaintext.  Detect them before ANY normalization, rotation, or
            # save: an old revoked file must never revoke a newer active
            # replacement merely because startup retried with stale local
            # state.
            current_entry = next(
                (
                    token
                    for token in store.tokens
                    if token.name == name and token.hash == token_hash
                ),
                None,
            )
            if current_entry is not None and not self._is_active(
                current_entry, now_dt
            ):
                return {
                    "status": "ok",
                    "name": name,
                    "registered": False,
                    "current_active": False,
                    "rotated_out": 0,
                    "permissions_normalized": 0,
                    "scopes_normalized": 0,
                }

            found_current = False
            rotated_out = 0
            perms_normalized = 0
            scopes_normalized = 0
            for t in store.tokens:
                if t.name != name:
                    continue  # JAMAIS toucher un token opérateur
                if t.hash == token_hash:
                    # Respecte un revoked opérateur explicite : pas de ré-activation.
                    found_current = True
                    # P7-8 : une entrée existante du nom RÉSERVÉ dont le scope a
                    # dérivé (ex. enregistrée élargie avant le verrou exact) est
                    # RAMENÉE à {'read','write'} — le nom réservé n'est jamais
                    # opérateur, et un scope élargi resterait sinon actif.
                    if set(t.permissions or []) != {"read", "write"}:
                        t.permissions = ["read", "write"]
                        perms_normalized += 1
                    if t.space_ids:
                        t.space_ids = []
                        scopes_normalized += 1
                elif not t.revoked:
                    t.revoked = True
                    rotated_out += 1

            if not found_current:
                store.tokens.append(
                    TokenInfo(
                        hash=token_hash,
                        name=name,
                        permissions=perms,
                        space_ids=[],
                        created_at=now,
                        expires_at=None,
                    )
                )

            if (
                not found_current
                or rotated_out
                or perms_normalized
                or scopes_normalized
            ):
                await self._save_store(store)

        return {
            "status": "ok",
            "name": name,
            "registered": not found_current,
            "current_active": True,
            "rotated_out": rotated_out,
            "permissions_normalized": perms_normalized,
            "scopes_normalized": scopes_normalized,
        }

    async def list_tokens(
        self,
        name_contains: str = "",
        has_space: str = "",
        include_revoked: bool = True,
    ) -> dict:
        """
        Liste les tokens sans plaintext. Le hash canonique complet est inclus
        comme identifiant de lifecycle admin et d'invitation exacte.

        Filtres optionnels (issue #13) appliqués in-memory sur la liste
        chargée depuis S3. Tous les defaults reproduisent le comportement
        antérieur (rétrocompat stricte).

        Args:
            name_contains: Sous-chaîne recherchée dans ``token.name``
                (insensible à la casse). Vide = pas de filtre.
            has_space: Filtre les tokens dont ``space_ids`` contient
                exactement ce ``space_id`` (match exact, sensible à la casse).
                Vide = pas de filtre.
            include_revoked: Si ``False``, exclut les tokens révoqués
                du résultat. Défaut ``True`` (comportement historique).

        Returns:
            ``{"status": "ok", "tokens": [...], "total": N, "filters": {...}}``
            (le bloc ``filters`` n'est ajouté que si au moins un filtre actif).
        """
        store = await self._load_store()

        # Préparation des filtres
        needle = name_contains.strip().lower() if name_contains else ""
        space_needle = has_space.strip() if has_space else ""

        tokens_list = []
        for t in store.tokens:
            # Filtre revoked
            if not include_revoked and t.revoked:
                continue
            # Filtre name_contains (case-insensitive)
            if needle and needle not in t.name.lower():
                continue
            # Filtre has_space (match exact)
            if space_needle and space_needle not in t.space_ids:
                continue

            tokens_list.append(
                {
                    "hash": t.hash,  # Hash complet pour identification
                    "name": t.name,
                    "email": t.email,
                    "permissions": t.permissions,
                    "space_ids": t.space_ids,
                    "created_at": t.created_at,
                    "expires_at": t.expires_at,
                    "last_used_at": t.last_used_at,
                    "revoked": t.revoked,
                }
            )

        response = {"status": "ok", "tokens": tokens_list, "total": len(tokens_list)}

        # Trace des filtres appliqués (utile pour debug / audit)
        active_filters = {}
        if name_contains:
            active_filters["name_contains"] = name_contains
        if has_space:
            active_filters["has_space"] = has_space
        if not include_revoked:
            active_filters["include_revoked"] = False
        if active_filters:
            response["filters"] = active_filters

        return response

    async def revoke_token(self, token_hash: str) -> dict:
        """
        Révoque un token (le rend inutilisable).

        VULN-03 fix : utilise _find_token_by_hash pour une correspondance
        sécurisée (min 16 chars, détection d'ambiguïté).

        LM2-07 fix : purge aussi le ``_fresh_token_store`` global pour
        empêcher toute opération longue (consolidation, push graph) en
        cours de continuer à voir les anciennes permissions.

        Args:
            token_hash: Hash SHA-256 du token (min 16 chars de préfixe)

        Returns:
            {"status": "ok"} ou erreur
        """
        async with get_lock_manager().tokens:
            store = await self._load_store()
            idx, token = self._find_token_by_hash(store, token_hash)
            err = self._token_not_found_or_ambiguous(idx, token_hash)
            if err:
                return err

            token.revoked = True
            full_hash = token.hash
            await self._save_store(store)

        # LM2-07 fix : invalider dans le store global après save_store
        self._invalidate_in_fresh_store([full_hash])

        return {"status": "ok", "message": f"Token '{token.name}' revoked"}

    async def delete_token(self, token_hash: str) -> dict:
        """
        Supprime physiquement un token du registre.

        VULN-03 fix : utilise _find_token_by_hash pour une correspondance
        sécurisée (min 16 chars, détection d'ambiguïté).

        Args:
            token_hash: Hash SHA-256 du token (min 16 chars de préfixe)

        Returns:
            {"status": "deleted", "name": "..."} ou erreur
        """
        async with get_lock_manager().tokens:
            store = await self._load_store()
            idx, token = self._find_token_by_hash(store, token_hash)
            err = self._token_not_found_or_ambiguous(idx, token_hash)
            if err:
                return err

            deleted_name = token.name
            deleted_hash = token.hash
            store.tokens.pop(idx)
            await self._save_store(store)

        # LM2-07 fix : purge du store global après save_store
        self._invalidate_in_fresh_store([deleted_hash])

        return {
            "status": "deleted",
            "name": deleted_name,
            "message": f"Token '{deleted_name}' permanently deleted",
            "remaining": len(store.tokens),
        }

    async def purge_tokens(self, revoked_only: bool = True) -> dict:
        """
        Supprime physiquement plusieurs tokens du registre.

        Args:
            revoked_only: Si True, ne supprime que les tokens révoqués.
                         Si False, supprime TOUS les tokens.

        Returns:
            {"status": "ok", "deleted": N, "remaining": M}
        """
        async with get_lock_manager().tokens:
            store = await self._load_store()
            original_count = len(store.tokens)

            # LM2-07 fix : collecter les hashes supprimés AVANT mutation
            # pour pouvoir purger le store global après save_store.
            if revoked_only:
                deleted_hashes = [t.hash for t in store.tokens if t.revoked]
                store.tokens = [t for t in store.tokens if not t.revoked]
            else:
                deleted_hashes = [t.hash for t in store.tokens]
                store.tokens = []

            deleted_count = original_count - len(store.tokens)
            await self._save_store(store)

        # LM2-07 fix : purge en masse du store global
        self._invalidate_in_fresh_store(deleted_hashes)

        return {
            "status": "ok",
            "deleted": deleted_count,
            "remaining": len(store.tokens),
            "mode": "revoked_only" if revoked_only else "all",
            "message": f"{deleted_count} token(s) permanently deleted",
        }

    # ─────────────────────────────────────────────────────────
    # Helpers privés pour les opérations de mise à jour (issue #13)
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_csv_spaces(value: str) -> list[str]:
        """Parse une chaîne CSV en liste dédupliquée, ordre préservé."""
        if not value:
            return []
        seen: set[str] = set()
        out: list[str] = []
        for raw in value.split(","):
            sid = raw.strip()
            if not sid:
                continue
            if sid in seen:
                continue
            seen.add(sid)
            out.append(sid)
        return out

    @staticmethod
    def _validate_update_mutex(
        space_ids: str, space_ids_add: str, space_ids_remove: str
    ) -> dict | None:
        """
        Vérifie l'exclusion mutuelle entre `space_ids` (remplacement) et
        `space_ids_add`/`space_ids_remove` (delta additif) — issue #13.

        Le sucre ``"*"``/``"all"`` reste valable uniquement pour
        ``space_ids`` (remplacement par snapshot). Il est interdit dans
        ``_add``/``_remove`` (un delta "tout ajouter / tout retirer" n'a
        pas de sémantique claire et serait piégeur).

        Retourne ``None`` si OK, sinon un dict d'erreur.
        """
        replace_active = bool((space_ids or "").strip())
        delta_active = bool((space_ids_add or "").strip()) or bool(
            (space_ids_remove or "").strip()
        )

        if replace_active and delta_active:
            return {
                "status": "error",
                "message": (
                    "Paramètres incompatibles : `space_ids` (remplacement) "
                    "et `space_ids_add`/`space_ids_remove` (delta additif) "
                    "ne peuvent pas être combinés. Choisissez l'un ou l'autre."
                ),
            }

        # Interdiction du sucre "*"/"all" dans les deltas (décision issue #13).
        for label, value in (
            ("space_ids_add", space_ids_add),
            ("space_ids_remove", space_ids_remove),
        ):
            stripped = (value or "").strip().lower()
            if stripped in ("*", "all"):
                return {
                    "status": "error",
                    "message": (
                        f"`{label}` n'accepte pas le sucre '*' / 'all' "
                        "(sémantique ambiguë sur un delta). Listez les "
                        "espaces explicitement ou utilisez `space_ids='*'` "
                        "pour un remplacement complet."
                    ),
                }

        return None

    @staticmethod
    def _apply_space_delta(
        current: list[str], to_add: list[str], to_remove: list[str]
    ) -> tuple[list[str], list[str], list[str], list[str]]:
        """
        Applique un delta additif sur une liste de space_ids.

        Sémantique :
        - ``to_add`` : chaque entrée non déjà présente est ajoutée.
        - ``to_remove`` : chaque entrée présente est retirée.
        - Idempotent : appels répétés ⇒ même résultat.
        - L'ordre relatif des entrées existantes est préservé.
        - ``_remove`` est appliqué AVANT ``_add`` (permet "remplacer X par Y"
          via `_add=Y,_remove=X` même si X==Y → effet net = présent).

        Returns:
            Tuple ``(new_list, actually_added, actually_removed, noop)`` :

            - ``new_list`` : liste résultante
            - ``actually_added`` : entrées effectivement ajoutées
            - ``actually_removed`` : entrées effectivement retirées
            - ``noop`` : entrées demandées mais sans effet (déjà
              présentes pour ``_add`` ou absentes pour ``_remove``)
        """
        actually_removed: list[str] = []
        noop: list[str] = []

        # Phase 1 : retraits
        working = list(current)
        for sid in to_remove:
            if sid in working:
                working.remove(sid)
                actually_removed.append(sid)
            else:
                noop.append(f"remove:{sid}")

        # Phase 2 : ajouts (en tête de liste préservée, append en queue)
        actually_added: list[str] = []
        for sid in to_add:
            if sid in working:
                noop.append(f"add:{sid}")
            else:
                working.append(sid)
                actually_added.append(sid)

        return working, actually_added, actually_removed, noop

    async def update_token(
        self,
        token_hash: str,
        space_ids: str = "",
        permissions: str = "",
        email: str = "",
        space_ids_add: str = "",
        space_ids_remove: str = "",
    ) -> dict:
        """
        Met à jour un token : permissions, email, et/ou ``space_ids``.

        **Trois modes pour ``space_ids``** (issue #13) :

        1. **Pas de changement** : aucun des trois paramètres
           ``space_ids``/``space_ids_add``/``space_ids_remove`` n'est fourni.
        2. **Remplacement complet** (legacy) : ``space_ids`` non vide.
           Accepte ``"*"``/``"all"`` (snapshot) ou une liste CSV.
        3. **Delta additif** (issue #13) : ``space_ids_add`` et/ou
           ``space_ids_remove`` non vides. Idempotent : ajouter un space
           déjà présent (ou retirer un absent) est un no-op. ``_remove``
           est appliqué avant ``_add``.

        Les modes (2) et (3) sont **mutuellement exclusifs** (erreur 400 si
        on les combine). Le sucre ``"*"``/``"all"`` n'est PAS supporté
        dans ``_add``/``_remove`` (sémantique ambiguë sur un delta).

        VULN-03 fix : ``_find_token_by_hash`` (min 16 chars, ambiguïté
        détectée). Review #12 : ``warning_no_access`` ajouté si le token
        résultant est muet (non-admin avec space_ids=[]).

        Args:
            token_hash: Hash du token (min 16 chars de préfixe)
            space_ids: Mode remplacement. ``""`` = pas de changement.
            permissions: Nouvelles permissions (vide = pas de changement)
            email: Nouvel email (vide = pas de changement)
            space_ids_add: Mode delta — espaces à ajouter (CSV).
            space_ids_remove: Mode delta — espaces à retirer (CSV).

        Returns:
            ``{"status": "ok", ...}`` avec, en mode delta, les champs
            ``space_ids_added``, ``space_ids_removed``, ``space_ids_noop``,
            ``space_ids_before``, ``space_ids_after``. ``warning_no_access``
            si le token devient muet.

        Invariant v2 : un token admin porte toujours ``space_ids=[]``. Une
        promotion admin efface son allowlist ; un downgrade depuis admin repart
        de [] et n'ajoute que le remplacement/delta explicitement fourni dans
        le même appel.
        """
        # Validation de l'exclusion mutuelle (avant toute lecture S3).
        mutex_err = self._validate_update_mutex(
            space_ids, space_ids_add, space_ids_remove
        )
        if mutex_err:
            return mutex_err

        # Pré-résoudre le sucre "*"/"all" hors du lock pour éviter
        # de tenir le verrou pendant un appel S3 (list_spaces).
        space_ids_stripped = (space_ids or "").strip()
        new_space_ids: Optional[list[str]] = None  # mode remplacement
        snapshot_used = False
        if space_ids_stripped:
            new_space_ids, snapshot_used = await self._resolve_space_ids(space_ids)

        # Parse des deltas (mode additif)
        add_list = self._parse_csv_spaces(space_ids_add)
        remove_list = self._parse_csv_spaces(space_ids_remove)
        delta_mode = bool(add_list) or bool(remove_list)

        async with get_lock_manager().tokens:
            store = await self._load_store()

            # Valider les permissions avant modification
            if permissions:
                perm_list = [p.strip() for p in permissions.split(",") if p.strip()]
                invalid = [p for p in perm_list if p not in VALID_PERMISSIONS]
                if invalid:
                    return {
                        "status": "error",
                        "message": (
                            f"Permissions invalides : {invalid}. "
                            f"Valeurs acceptées : {sorted(VALID_PERMISSIONS)}"
                        ),
                    }

            idx, token = self._find_token_by_hash(store, token_hash)
            err = self._token_not_found_or_ambiguous(idx, token_hash)
            if err:
                return err

            # Snapshot du before (pour traçabilité delta)
            before_space_ids = list(token.space_ids)
            before_permissions = list(token.permissions)
            before_is_admin = "admin" in set(token.permissions or [])

            actually_added: list[str] = []
            actually_removed: list[str] = []
            noop_entries: list[str] = []

            if permissions:
                token.permissions = [
                    p.strip() for p in permissions.split(",") if p.strip()
                ]
            resulting_is_admin = "admin" in set(token.permissions or [])

            # Un scope porté par un admin est dormant mais peut devenir actif
            # lors d'un downgrade. Le v2 l'interdit. Une transition depuis
            # admin repart donc de [] ; un remplacement/delta explicitement
            # fourni au même appel construit le nouveau scope non-admin depuis
            # cette base vide. Une promotion admin efface toujours le scope.
            scope_base = [] if before_is_admin else list(token.space_ids)
            if resulting_is_admin:
                token.space_ids = []
                actually_removed = list(before_space_ids)
            elif new_space_ids is not None:
                # Mode remplacement complet
                token.space_ids = new_space_ids
            elif delta_mode:
                # Mode delta additif
                (
                    token.space_ids,
                    actually_added,
                    actually_removed,
                    noop_entries,
                ) = self._apply_space_delta(scope_base, add_list, remove_list)
                if before_is_admin:
                    actually_removed = list(before_space_ids) + [
                        sid for sid in actually_removed if sid not in before_space_ids
                    ]
            elif before_is_admin:
                # Downgrade sans scope explicite : aucun droit dormant ne fuit.
                token.space_ids = []
            if email:
                token.email = email

            await self._save_store(store)
            # Snapshot des champs nécessaires pour la réponse (avant sortie du lock)
            updated_name = token.name
            updated_hash = token.hash
            updated_space_ids = list(token.space_ids)
            updated_perms = list(token.permissions)

        # LM2-07 fix : purger le store global si les droits effectifs ont
        # été modifiés (permissions ou space_ids). L'email seul n'affecte
        # pas l'autorisation runtime → pas d'invalidation nécessaire.
        if permissions or new_space_ids is not None or delta_mode:
            self._invalidate_in_fresh_store([updated_hash])

        response = {
            "status": "ok",
            "message": f"Token '{updated_name}' updated",
        }

        if _authorization_profile_changed(
            before_permissions,
            updated_perms,
            before_space_ids,
            updated_space_ids,
        ):
            # Authorization uses fresh state on the next call.  Discovery may
            # be cached client-side, so an effective permission/scope change
            # explicitly requests an MCP reconnect.
            response["mcp_reconnect_required"] = True

        space_ids_touched = (
            (new_space_ids is not None)
            or delta_mode
            or before_space_ids != updated_space_ids
            # A downgrade is itself an effective scope transition: admin was
            # global, while the resulting non-admin token can access only its
            # (possibly empty) allowlist.  Report the empty result even when
            # both serialized lists are [] under the v2 admin invariant.
            or (before_is_admin and not resulting_is_admin)
        )

        # Review #12 : signaler un token muet (cohérent avec create_token)
        # uniquement si space_ids a été touché par cet appel.
        if space_ids_touched:
            is_admin = "admin" in updated_perms
            if not is_admin and not updated_space_ids:
                response["warning_no_access"] = self._muted_token_warning()

        if resulting_is_admin:
            snapshot_used = False
            if space_ids_touched:
                response["scope_normalized"] = True
                response["info"] = (
                    "Un token admin est global par permission : space_ids a été "
                    "stocké vide pour empêcher un scope dormant."
                )

        if snapshot_used:
            response["snapshot_taken"] = True
            response["info"] = (
                f"space_ids='{space_ids_stripped}' interprété comme snapshot "
                f"des {len(updated_space_ids)} espace(s) existant(s) au moment "
                "de la mise à jour. Les futurs nouveaux espaces ne seront PAS "
                "automatiquement ajoutés."
            )

        if delta_mode:
            response["mode"] = "delta"
            response["space_ids_before"] = before_space_ids
            response["space_ids_after"] = updated_space_ids
            response["space_ids_added"] = actually_added
            response["space_ids_removed"] = actually_removed
            if noop_entries:
                response["space_ids_noop"] = noop_entries

        return response

    async def bulk_update_tokens(
        self,
        names: str = "",
        name_contains: str = "",
        has_space: str = "",
        permissions: str = "",
        email: str = "",
        space_ids_add: str = "",
        space_ids_remove: str = "",
        include_revoked: bool = False,
    ) -> dict:
        """
        Met à jour plusieurs tokens en une seule opération (issue #13).

        **Atomicité** : tokens.json est un fichier S3 unique chargé/sauvé
        sous lock asyncio. Toutes les modifications sont appliquées en
        mémoire, validées, puis une seule écriture finale. En cas d'erreur
        de validation (ex: permissions invalides), AUCUNE modification
        n'est persistée. ⚠️ Atomicité garantie *au sein d'une instance
        MCP* — un déploiement HA multi-instances nécessiterait un verrou
        externe (Redis/etcd), non implémenté (déploiement single-instance).

        **Filtres** (au moins un de ``names``/``name_contains``/``has_space``
        requis) :

        - ``names`` : liste CSV de noms exacts à matcher.
        - ``name_contains`` : sous-chaîne dans le nom (case-**insensitive**).
        - ``has_space`` : space_id présent dans ``token.space_ids``
          (match exact, case-**sensitive**, cohérent avec ``list_tokens``).
          ⚠️ Asymétrie volontaire : les noms sont libres et le matching
          tolérant à la casse aide l'opérateur ; les ``space_ids`` sont
          des identifiants techniques et la casse est significative.

        ⚠️ **Tous les filtres fournis sont combinés en AND** : un token
        doit satisfaire **chacun**. Exemple piège : ``names="a,b,c"`` +
        ``name_contains="agent"`` n'inclura PAS un token nommé ``"c"``
        s'il ne contient pas ``"agent"``. Pour une logique OR, faites
        plusieurs appels.

        **Filtre sécurité — ``include_revoked``** (review PR #14) :

        - Défaut ``False`` : les tokens révoqués matchés par les filtres
          sont **sautés** et listés dans ``skipped_revoked``. Asymétrie
          volontaire avec ``list_tokens(include_revoked=True)`` — la
          sémantique d'usage diffère : on **observe** vs on **mute**.
          Modifier un token révoqué n'a aucun effet pratique mais peut
          créer des permissions fantômes en cas de ré-activation.
        - ``True`` : opt-in explicite pour modifier aussi les révoqués
          (ex: réhabilitation avant ré-activation).

        **Opérations** (au moins une requise, sinon erreur 400) :

        - ``permissions`` : nouvelles permissions à appliquer.
        - ``email`` : nouvel email.
        - ``space_ids_add`` / ``space_ids_remove`` : deltas additifs
          idempotents (mêmes règles que ``update_token`` en mode delta).
          Le cas dégénéré ``_add=X, _remove=X`` retire puis ré-ajoute X :
          effet net = X présent en queue de liste.

        ⚠️ Volontairement, ``bulk_update_tokens`` n'expose **pas** le
        mode remplacement ``space_ids`` (trop dangereux à propager sur N
        tokens — risque de révocation silencieuse en masse).

        Invariant v2 : une promotion admin efface les scopes. Un downgrade
        depuis admin repart de [] avant d'appliquer le delta explicite ; sans
        delta, le token non-admin devient volontairement muet.

        **Audit** (review PR #14, point #4) : un événement structuré
        ``event="bulk_update_tokens"`` est émis sur le logger
        ``live_mem.audit`` après l'écriture S3 (succès garanti). Permet
        la rejouabilité même si le retour MCP est perdu côté client.

        Args:
            names: Noms exacts à filtrer (CSV).
            name_contains: Sous-chaîne case-insensitive dans le nom.
            has_space: Space_id présent dans space_ids (match exact).
            permissions: Nouvelles permissions (CSV) à appliquer.
            email: Nouvel email à appliquer.
            space_ids_add: Spaces à ajouter (CSV).
            space_ids_remove: Spaces à retirer (CSV).
            include_revoked: Inclure les tokens révoqués (défaut False).

        Returns:
            ``{"status": "ok", "updated": N, "tokens": [...],
            "skipped_revoked": [{"name", "hash"}], "filters": {...},
            "operations": {...}}``.
            ``skipped_revoked`` n'est présent que si au moins un token
            révoqué a été matché par les filtres (mais pas modifié).
            Si aucun token ne matche : ``updated=0``, statut ``ok``.
        """
        # ─── Validation des filtres ───
        names_list = [n.strip() for n in (names or "").split(",") if n.strip()]
        name_contains_norm = (name_contains or "").strip()
        has_space_norm = (has_space or "").strip()
        if not names_list and not name_contains_norm and not has_space_norm:
            return {
                "status": "error",
                "message": (
                    "Au moins un filtre requis : `names` (liste exacte), "
                    "`name_contains` (sous-chaîne) ou `has_space` (space_id)."
                ),
            }

        # ─── Validation des opérations ───
        # Note : `space_ids` (remplacement) volontairement absent — voir docstring.
        op_perm = (permissions or "").strip()
        op_email = (email or "").strip()
        add_list = self._parse_csv_spaces(space_ids_add)
        remove_list = self._parse_csv_spaces(space_ids_remove)

        if not (op_perm or op_email or add_list or remove_list):
            return {
                "status": "error",
                "message": (
                    "Aucune opération demandée. Fournissez au moins "
                    "`permissions`, `email`, `space_ids_add` ou `space_ids_remove`."
                ),
            }

        # Valider le sucre interdit "*"/"all" dans les deltas (avant lock).
        mutex_err = self._validate_update_mutex("", space_ids_add, space_ids_remove)
        if mutex_err:
            return mutex_err

        # Valider les permissions à plat (avant lock).
        if op_perm:
            perm_list = [p.strip() for p in op_perm.split(",") if p.strip()]
            invalid = [p for p in perm_list if p not in VALID_PERMISSIONS]
            if invalid:
                return {
                    "status": "error",
                    "message": (
                        f"Permissions invalides : {invalid}. "
                        f"Valeurs acceptées : {sorted(VALID_PERMISSIONS)}"
                    ),
                }
        else:
            perm_list = None  # signal "ne pas toucher"

        # ─── Application sous lock ───
        needle = name_contains_norm.lower()
        async with get_lock_manager().tokens:
            store = await self._load_store()

            # Sélection des tokens matchant les filtres (AND-combinés).
            # Les révoqués matchés sont mis à part dans `skipped_revoked`
            # quand include_revoked=False (review PR #14, point #3).
            selected: list[TokenInfo] = []
            skipped_revoked: list[dict] = []
            for t in store.tokens:
                if names_list and t.name not in names_list:
                    continue
                if needle and needle not in t.name.lower():
                    continue
                if has_space_norm and has_space_norm not in t.space_ids:
                    continue
                # Filtres satisfaits : décider de l'inclusion selon revoked.
                if t.revoked and not include_revoked:
                    skipped_revoked.append({"name": t.name, "hash": t.hash})
                    continue
                selected.append(t)

            filters_block = {
                "names": names_list,
                "name_contains": name_contains_norm,
                "has_space": has_space_norm,
                "include_revoked": include_revoked,
            }

            if not selected:
                response: dict = {
                    "status": "ok",
                    "updated": 0,
                    "tokens": [],
                    "filters": filters_block,
                }
                if skipped_revoked:
                    response["skipped_revoked"] = skipped_revoked
                    response["message"] = (
                        f"Aucun token actif ne correspond — "
                        f"{len(skipped_revoked)} token(s) révoqué(s) "
                        "matché(s) mais sautés (utilisez include_revoked=True "
                        "pour les inclure)."
                    )
                else:
                    response["message"] = "No tokens match the filters."
                return response

            # Application en mémoire (atomique : aucune écriture S3 tant que
            # toutes les modifs ne sont pas faites).
            report: list[dict] = []
            for t in selected:
                before_space_ids = list(t.space_ids)
                before_perms = list(t.permissions)
                before_email = t.email
                before_is_admin = "admin" in set(before_perms)

                if perm_list is not None:
                    t.permissions = list(perm_list)
                if op_email:
                    t.email = op_email

                added: list[str] = []
                removed: list[str] = []
                noop: list[str] = []
                resulting_is_admin = "admin" in set(t.permissions or [])
                scope_base = [] if before_is_admin else list(t.space_ids)
                if resulting_is_admin:
                    # Promotion ou maintien admin : aucune allowlist dormante.
                    t.space_ids = []
                    removed = list(before_space_ids)
                elif add_list or remove_list:
                    t.space_ids, added, removed, noop = self._apply_space_delta(
                        scope_base, add_list, remove_list
                    )
                    if before_is_admin:
                        removed = list(before_space_ids) + [
                            sid for sid in removed if sid not in before_space_ids
                        ]
                elif before_is_admin:
                    # Downgrade bulk sans delta : le nouveau token repart muet.
                    t.space_ids = []

                entry: dict = {
                    "name": t.name,
                    "hash": t.hash,
                    "before": {
                        "space_ids": before_space_ids,
                        "permissions": before_perms,
                        "email": before_email,
                    },
                    "after": {
                        "space_ids": list(t.space_ids),
                        "permissions": list(t.permissions),
                        "email": t.email,
                    },
                }
                if add_list or remove_list:
                    entry["space_ids_added"] = added
                    entry["space_ids_removed"] = removed
                    if noop:
                        entry["space_ids_noop"] = noop
                report.append(entry)

            # Une seule écriture S3 ⇒ atomicité naturelle
            await self._save_store(store)

        # LM2-07 fix : purger le store global pour tous les tokens dont les
        # droits ont été modifiés (permissions ou space_ids touchés). L'email
        # seul n'affecte pas l'autorisation runtime → skip pour économiser.
        if perm_list is not None or add_list or remove_list:
            self._invalidate_in_fresh_store([entry["hash"] for entry in report])

        # ─── Audit logging (review PR #14, point #4) ───
        # Émis APRÈS le save_store : on ne loggue que les opérations
        # effectivement persistées. Les échecs de validation ne polluent
        # pas l'audit (ils ont leur retour MCP côté client).
        self._emit_bulk_update_audit(
            filters=filters_block,
            operations={
                "permissions": perm_list,
                "email": op_email or None,
                "space_ids_add": add_list,
                "space_ids_remove": remove_list,
            },
            updated=len(report),
            token_hashes=[entry["hash"] for entry in report],
            skipped_revoked=skipped_revoked,
        )

        response = {
            "status": "ok",
            "updated": len(report),
            "tokens": report,
            "filters": filters_block,
            "operations": {
                "permissions": perm_list,
                "email": op_email or None,
                "space_ids_add": add_list,
                "space_ids_remove": remove_list,
            },
        }
        if any(
            _authorization_profile_changed(
                entry["before"]["permissions"],
                entry["after"]["permissions"],
                entry["before"]["space_ids"],
                entry["after"]["space_ids"],
            )
            for entry in report
        ):
            response["mcp_reconnect_required"] = True
        if skipped_revoked:
            response["skipped_revoked"] = skipped_revoked
        return response

    @staticmethod
    def _emit_bulk_update_audit(
        *,
        filters: dict,
        operations: dict,
        updated: int,
        token_hashes: list[str],
        skipped_revoked: list[dict],
    ) -> None:
        """
        Émet un événement d'audit structuré sur le logger ``live_mem.audit``.

        Appelé après une opération ``bulk_update_tokens`` réussie. Le
        contenu permet de rejouer/auditer l'opération a posteriori :
        identité du caller, filtres, opérations, cibles, sautés.

        L'identité du caller et le request_id sont récupérés depuis les
        ``ContextVar`` du middleware (best effort — fallback "system" si
        appel hors-requête HTTP, par ex. via tests unitaires ou CLI).
        """
        # Imports locaux pour éviter les cycles et permettre les tests
        # qui n'instancient pas le middleware stack complet.
        try:
            from ..auth.context import current_token_info
            from ..middleware import current_request_id

            tinfo = current_token_info.get()
            caller = tinfo.get("client_name", "unknown") if tinfo else "system"
            req_id = current_request_id.get()
        except Exception:
            caller = "system"
            req_id = "-"

        entry = {
            "event": "bulk_update_tokens",
            "request_id": req_id,
            "caller": caller,
            "filters": filters,
            "operations": operations,
            "updated": updated,
            "token_hashes": token_hashes,
            "skipped_revoked_count": len(skipped_revoked),
        }
        # Best effort : un échec de logging ne doit pas casser l'opération
        # (le store S3 est déjà sauvé à ce stade).
        try:
            _audit_logger.info(json.dumps(entry, ensure_ascii=False))
        except Exception:
            pass

    async def validate_token(self, raw_token: str) -> Optional[dict]:
        """
        Valide un token brut et retourne ses infos.

        Appelé par le middleware d'authentification à chaque requête.

        VULN-01 (audit v1.0.0) : l'écriture de last_used_at a été supprimée
        de cette méthode pour éliminer la race condition avec les opérations
        sous lock (create/revoke/update). last_used_at est désormais mis à
        jour de manière différée via _update_last_used().

        Args:
            raw_token: Token en clair (ex: "lm_a1B2c3...")

        Returns:
            Dict avec client_name, permissions, allowed_resources
            ou None si le token est invalide/révoqué/expiré
        """
        # Calculer le hash
        token_hash = "sha256:" + hashlib.sha256(raw_token.encode()).hexdigest()

        # Charger le store
        store = await self._load_store()
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        for t in store.tokens:
            if t.hash != token_hash:
                continue

            # Vérifier révocation
            if t.revoked:
                return None

            # Vérifier expiration
            # HM-20 fix : comparaison en DATETIME, pas lexicographique sur str.
            # L'ancien `t.expires_at < now` (comparaison de chaînes ISO 8601) n'est
            # correct que si TOUS les writers utilisent le même format (isoformat
            # UTC +00:00). Un futur writer avec suffixe `Z`, une tz locale ou une
            # précision µs différente cassait silencieusement la comparaison →
            # token expiré vu comme valide (fail-open). On parse ; un expires_at
            # illisible est traité comme EXPIRÉ (fail-closed).
            if t.expires_at and _token_is_expired(t.expires_at, now_dt):
                return None

            # Token valide — mise à jour last_used_at différée (en mémoire)
            # VULN-01 fix : on ne fait plus _save_store() ici pour éviter
            # la race condition avec create/revoke/update qui sont sous lock.
            self._last_used_cache[token_hash] = now

            return {
                "type": "token",
                "client_name": t.name,
                "permissions": t.permissions,
                "allowed_resources": t.space_ids,
                "token_hash": t.hash,
            }

        return None  # Token inconnu

    async def migrate_empty_space_ids(self, all_space_ids: list[str]) -> dict:
        """
        Migration versionnée one-shot v1 -> v2 des ``space_ids=[]``.

        Dans un store v1, ``space_ids=[]`` signifiait historiquement "tous" :
        chaque token non-admin concerné reçoit donc un snapshot des espaces
        existants. Le marker v2 est ensuite écrit même si le store ou la liste
        des espaces est vide. Dans un store v2, ``[]`` signifie définitivement
        "aucun accès" et cette méthode est un no-op : un token nouvellement créé
        ne pourra ainsi jamais être ré-élargi lors d'un redémarrage.

        Les tokens admin sont normalisés vers ``space_ids=[]`` : leur bypass
        rend ces scopes inutiles et un downgrade futur ne doit jamais les
        réactiver silencieusement.

        Args:
            all_space_ids: Liste de tous les space_ids existants

        Returns:
            {"status": "ok", "migrated": N, "skipped": M}
        """
        async with get_lock_manager().tokens:
            storage = get_storage()
            raw = await storage.get_json(TOKENS_KEY)
            store = self._store_from_data(raw, allow_legacy_v1=True)

            # v2 prouve durablement que [] signifie déjà "aucun accès". Ne
            # jamais ré-élargir ces tokens lors d'un redémarrage ultérieur.
            if raw is not None and store.version == CURRENT_TOKENS_VERSION:
                return {
                    "status": "ok",
                    "migrated": 0,
                    "skipped": len(store.tokens),
                    "total_spaces": len(all_space_ids),
                    "already_migrated": True,
                    "admin_scopes_cleared": 0,
                }

            migrated = 0
            skipped = 0
            admin_scopes_cleared = 0

            for t in store.tokens:
                # Le v2 interdit tout scope dormant sur un admin, y compris
                # révoqué : la migration doit nettoyer avant d'écrire le marker.
                if "admin" in t.permissions:
                    if t.space_ids:
                        t.space_ids = []
                        admin_scopes_cleared += 1
                    skipped += 1
                    continue
                if t.revoked:
                    skipped += 1
                    continue
                # Déjà peuplé → rien à faire
                if t.space_ids:
                    skipped += 1
                    continue
                # Token avec space_ids=[] (ancien "accès à tous")
                # → leur donner tous les espaces existants
                t.space_ids = list(all_space_ids)
                migrated += 1

            # Même zéro token / zéro espace doit écrire le marker v2. Sans ce
            # commit, chaque restart réinterpréterait de futurs [] comme legacy.
            store.version = CURRENT_TOKENS_VERSION
            await self._save_store(store)

        return {
            "status": "ok",
            "migrated": migrated,
            "skipped": skipped,
            "total_spaces": len(all_space_ids),
            "already_migrated": False,
            "admin_scopes_cleared": admin_scopes_cleared,
        }

    # ─────────────────────────────────────────────────────────
    # Helpers internes
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _store_from_data(
        data: Optional[dict], *, allow_legacy_v1: bool = False
    ) -> TokensStore:
        """Valide la version du registre de tokens (état auth critique).

        Un store absent est créé directement au format courant. Les versions
        inconnues, futures ou non entières échouent fermé. Le v2 interdit aussi
        tout ``space_ids`` non vide sur un admin. Le v1 n'est accepté que par
        le migrateur one-shot explicite, qui normalise ces scopes avant commit.
        """
        if data is None:
            return TokensStore(version=CURRENT_TOKENS_VERSION)
        if not isinstance(data, dict):
            raise RuntimeError("Corrupted token registry: expected a JSON object")
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            raise RuntimeError("Corrupted token registry: version must be an integer")
        if version < LEGACY_TOKENS_VERSION or version > CURRENT_TOKENS_VERSION:
            raise RuntimeError(f"Version de registre tokens non supportée : {version}")
        if version == LEGACY_TOKENS_VERSION and not allow_legacy_v1:
            raise RuntimeError(
                "Registre tokens v1 non migré : démarrage/migration v2 requis"
            )

        raw_tokens = data.get("tokens")
        if not isinstance(raw_tokens, list):
            raise RuntimeError(
                "Registre de tokens corrompu : liste tokens requise"
            )
        seen_hashes: set[str] = set()
        for index, raw_token in enumerate(raw_tokens):
            label = f"token[{index}]"
            if not isinstance(raw_token, dict):
                raise RuntimeError(
                    f"Registre de tokens corrompu : {label} doit être un objet"
                )

            token_hash = raw_token.get("hash")
            if not isinstance(token_hash, str) or not _CANONICAL_TOKEN_HASH_RE.fullmatch(
                token_hash
            ):
                raise RuntimeError(
                    f"Registre de tokens corrompu : hash canonique requis pour {label}"
                )
            if token_hash in seen_hashes:
                raise RuntimeError(
                    f"Registre de tokens corrompu : hash dupliqué pour {label}"
                )
            seen_hashes.add(token_hash)

            raw_permissions = raw_token.get("permissions")
            if not isinstance(raw_permissions, list):
                raise RuntimeError(
                    f"Registre de tokens corrompu : permissions liste requise pour {label}"
                )
            if any(not isinstance(item, str) for item in raw_permissions):
                raise RuntimeError(
                    f"Registre de tokens corrompu : permission non textuelle pour {label}"
                )
            if any(item not in VALID_PERMISSIONS for item in raw_permissions):
                raise RuntimeError(
                    f"Registre de tokens corrompu : permission inconnue pour {label}"
                )
            if len(set(raw_permissions)) != len(raw_permissions):
                raise RuntimeError(
                    f"Registre de tokens corrompu : permission dupliquée pour {label}"
                )

            raw_space_ids = raw_token.get("space_ids")
            if not isinstance(raw_space_ids, list):
                raise RuntimeError(
                    f"Registre de tokens corrompu : space_ids liste requise pour {label}"
                )
            if any(not isinstance(item, str) for item in raw_space_ids):
                raise RuntimeError(
                    f"Registre de tokens corrompu : space_id non textuel pour {label}"
                )
            if any(
                not _CANONICAL_SPACE_ID_RE.fullmatch(item) for item in raw_space_ids
            ):
                raise RuntimeError(
                    f"Registre de tokens corrompu : space_id invalide pour {label}"
                )
            if len(set(raw_space_ids)) != len(raw_space_ids):
                raise RuntimeError(
                    f"Registre de tokens corrompu : space_id dupliqué pour {label}"
                )
            if (
                version == CURRENT_TOKENS_VERSION
                and "admin" in raw_permissions
                and raw_space_ids
            ):
                raise RuntimeError(
                    "Registre de tokens corrompu : un token admin v2 doit "
                    f"avoir space_ids vide pour {label}"
                )

            if "revoked" in raw_token and not isinstance(raw_token["revoked"], bool):
                raise RuntimeError(
                    f"Registre de tokens corrompu : revoked booléen requis pour {label}"
                )
            if "expires_at" in raw_token and not (
                raw_token["expires_at"] is None
                or isinstance(raw_token["expires_at"], str)
            ):
                raise RuntimeError(
                    f"Registre de tokens corrompu : expires_at texte/null requis pour {label}"
                )
            if "last_used_at" in raw_token and not (
                raw_token["last_used_at"] is None
                or isinstance(raw_token["last_used_at"], str)
            ):
                raise RuntimeError(
                    f"Registre de tokens corrompu : last_used_at texte/null requis pour {label}"
                )
            for text_field in ("name", "email", "created_at"):
                if text_field in raw_token and not isinstance(
                    raw_token[text_field], str
                ):
                    raise RuntimeError(
                        "Registre de tokens corrompu : "
                        f"{text_field} textuel requis pour {label}"
                    )
        return TokensStore(**data)

    async def _load_store(self) -> TokensStore:
        """Charge le registre courant depuis S3, fail-closed sur v1/inconnu."""
        storage = get_storage()
        data = await storage.get_json(TOKENS_KEY)
        return self._store_from_data(data)

    async def _save_store(self, store: TokensStore) -> None:
        """Valide puis sauvegarde toujours au schéma courant."""
        store.version = CURRENT_TOKENS_VERSION
        data = store.model_dump()
        # Une mutation interne/admin ne doit jamais produire un v2 que le
        # prochain load refuserait. Validation avant tout PUT durable.
        self._store_from_data(data)
        storage = get_storage()
        await storage.put_json(TOKENS_KEY, data)


# ─────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────

_token_service: TokenService | None = None


def get_token_service() -> TokenService:
    """Retourne le singleton TokenService."""
    global _token_service
    if _token_service is None:
        _token_service = TokenService()
    return _token_service
