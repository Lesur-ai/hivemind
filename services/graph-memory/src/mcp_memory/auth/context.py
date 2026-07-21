# -*- coding: utf-8 -*-
"""
Auth Context - Propagation du contexte d'authentification.

Utilise contextvars pour propager les infos d'auth du middleware ASGI
vers les outils MCP (qui n'ont pas accès au scope ASGI).

Usage dans le middleware:
    from .context import current_auth
    current_auth.set({"client_name": "quoteflow", "memory_ids": ["JURIDIQUE"]})

Usage dans les outils:
    from .auth.context import current_auth
    auth = current_auth.get()  # None si pas d'auth (localhost, public paths)
"""

import contextvars
from typing import Optional, Dict, Any

# ContextVar initialisé à None (pas d'auth = accès libre, cas localhost)
current_auth: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    'current_auth', default=None
)

# P7-4 (ADR-0019, Hivemind): explicit deny sentinel for list helpers when there
# is NO auth context. Distinct from None (admin/bootstrap = unrestricted) so a
# missing/rejected credential can never be conflated with admin and silently
# list everything (fail-closed).
DENY_ALL = object()


def check_memory_access(memory_id: str) -> Optional[dict]:
    """
    Vérifie si le contexte d'auth actuel autorise l'accès à une mémoire.
    
    Sécurité v2.1.0 : valide aussi le format de memory_id (anti injection).
    
    Règles :
    - Format memory_id invalide → refusé (ValueError)
    - Pas d'auth (localhost, public) → autorisé
    - Auth avec memory_ids vide → accès à toutes les mémoires
    - Auth avec memory_ids renseigné → accès restreint
    - Permission "admin" → toujours autorisé
    
    Args:
        memory_id: ID de la mémoire à vérifier
        
    Returns:
        None si autorisé, dict d'erreur si refusé
    """
    # P7-4 (ADR-0019, Hivemind): fail-closed FIRST — no auth context => DENY
    # (was: allow). Checked before format validation so the no-auth path never
    # depends on importing the validators/core stack, and a missing/rejected
    # credential is denied before any other work.
    auth = current_auth.get()
    if auth is None:
        return {"status": "error", "message": "Authentification requise"}

    # Sécurité v2.1.0 : valider le format de memory_id avant tout accès
    # Empêche les injections Cypher/S3/Qdrant via memory_id malveillant
    try:
        from ..core.validators import validate_memory_id
        validate_memory_id(memory_id)
    except ValueError as e:
        return {"status": "error", "message": str(e)}

    # Admin = accès total
    if "admin" in auth.get("permissions", []):
        return None
    
    # Bootstrap key = accès total
    if auth.get("type") == "bootstrap":
        return None
    
    # memory_ids vide = accès à toutes les mémoires
    memory_ids = auth.get("memory_ids", [])
    if not memory_ids:
        return None
    
    # Vérifier que la mémoire est dans la liste autorisée
    if memory_id not in memory_ids:
        client = auth.get("client_name", "inconnu")
        return {
            "status": "error",
            "message": (
                f"Accès refusé: le token du client '{client}' "
                f"n'est pas autorisé pour la mémoire '{memory_id}'. "
                f"Mémoires autorisées: {memory_ids}"
            )
        }
    
    return None  # Autorisé


def check_admin_permission() -> Optional[dict]:
    """
    Vérifie si le contexte d'auth actuel a la permission admin.
    
    Règles :
    - Pas d'auth (localhost, public) → autorisé (accès libre)
    - Permission "admin" → autorisé
    - Bootstrap key → autorisé
    - Sinon → refusé
    
    Returns:
        None si autorisé, dict d'erreur si refusé
    """
    auth = current_auth.get()
    
    # P7-4 (ADR-0019, Hivemind): fail-closed — no auth context => DENY (was: allow).
    if auth is None:
        return {"status": "error", "message": "Authentification requise"}

    # Bootstrap = admin
    if auth.get("type") == "bootstrap":
        return None
    
    permissions = auth.get("permissions", [])
    if "admin" in permissions:
        return None
    
    client = auth.get("client_name", "inconnu")
    return {
        "status": "error",
        "message": (
            f"Accès refusé: le token du client '{client}' "
            f"n'a pas la permission 'admin'. "
            f"Cette opération est réservée aux administrateurs."
        )
    }


def get_allowed_memory_ids() -> Optional[list]:
    """
    Retourne la liste des memory_ids autorisés pour le contexte d'auth actuel.
    
    Returns:
        DENY_ALL si pas d'auth (fail-closed — les helpers de liste ne montrent rien)
        None si admin/bootstrap (= pas de restriction)
        [] si memory_ids vide dans le token (= toutes les mémoires autorisées)
        ["A", "B"] si restriction à des mémoires spécifiques
    """
    auth = current_auth.get()

    # P7-4 (ADR-0019): fail-closed — no auth context => DENY (distinct from None
    # so list callers never conflate "no auth" with "admin/bootstrap").
    if auth is None:
        return DENY_ALL

    # Admin ou bootstrap = accès total (pas de filtrage)
    if auth.get("type") == "bootstrap":
        return None
    if "admin" in auth.get("permissions", []):
        return None
    
    # Retourner la liste (peut être [] = toutes)
    return auth.get("memory_ids", [])


def check_write_permission() -> Optional[dict]:
    """
    Vérifie si le contexte d'auth actuel a la permission d'écriture.
    
    Règles :
    - Pas d'auth (localhost, public) → autorisé (accès libre)
    - Permission "admin" ou "write" → autorisé
    - Bootstrap key → autorisé
    - Sinon → refusé
    
    Returns:
        None si autorisé, dict d'erreur si refusé
    """
    auth = current_auth.get()
    
    # P7-4 (ADR-0019, Hivemind): fail-closed — no auth context => DENY (was: allow).
    if auth is None:
        return {"status": "error", "message": "Authentification requise"}

    # Admin ou bootstrap = accès total
    if auth.get("type") == "bootstrap":
        return None
    
    permissions = auth.get("permissions", [])
    if "admin" in permissions or "write" in permissions:
        return None
    
    client = auth.get("client_name", "inconnu")
    return {
        "status": "error",
        "message": (
            f"Accès refusé: le token du client '{client}' "
            f"n'a pas la permission 'write'. "
            f"Permissions actuelles: {permissions}"
        )
    }
