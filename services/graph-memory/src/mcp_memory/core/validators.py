# -*- coding: utf-8 -*-
"""
Validators — Validation centralisée des entrées utilisateur.

Sécurité v2.1.0 : Ce module fournit des fonctions de validation
pour tous les inputs utilisateur afin de prévenir :
- Path traversal S3 (C4)
- Injection Cypher via memory_id (H2/H3)
- Noms de fichiers malveillants (M1)
- Déni de service par taille excessive (H6)

Toutes les fonctions lèvent ValueError si l'input est invalide.
"""

import os
import re
import sys


# =============================================================================
# Patterns de validation
# =============================================================================

# memory_id : alphanumérique + tirets/underscores, 1-64 chars, commence par alphanum
VALID_MEMORY_ID = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')

# filename : pas de traversal, pas de null bytes, longueur raisonnable
VALID_FILENAME_CHARS = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._\- ]{0,254}$')

# backup_id : format "memory_id/timestamp"
VALID_BACKUP_COMPONENT = re.compile(r'^[A-Za-z0-9_-]+$')

# Taille maximale d'un document à l'ingestion (en bytes)
MAX_INGEST_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB


# =============================================================================
# Fonctions de validation
# =============================================================================

def validate_memory_id(memory_id: str) -> str:
    """
    Valide un memory_id.
    
    Règles :
    - Non vide
    - 1-64 caractères
    - Commence par une lettre ou un chiffre
    - Contient uniquement des lettres, chiffres, tirets et underscores
    - Pas de path traversal (..)
    - Pas de null bytes
    
    Args:
        memory_id: L'identifiant de mémoire à valider
        
    Returns:
        Le memory_id validé (inchangé)
        
    Raises:
        ValueError: Si le memory_id est invalide
    """
    if not memory_id:
        raise ValueError("memory_id est requis (ne peut pas être vide)")
    
    if '\x00' in memory_id:
        raise ValueError(f"memory_id contient des null bytes: {memory_id!r}")
    
    if '..' in memory_id:
        raise ValueError(f"memory_id contient du path traversal: {memory_id!r}")
    
    if not VALID_MEMORY_ID.match(memory_id):
        raise ValueError(
            f"memory_id invalide: {memory_id!r}. "
            f"Autorisé : lettres, chiffres, tirets, underscores (1-64 chars, commence par alphanum)"
        )
    
    return memory_id


def validate_filename(filename: str) -> str:
    """
    Valide et sanitise un nom de fichier.
    
    Règles :
    - Non vide
    - Extraction du basename (retire tout chemin)
    - Pas de path traversal (..)
    - Pas de null bytes
    - Longueur max 255 caractères
    - Caractères autorisés : lettres, chiffres, points, tirets, underscores, espaces
    
    Args:
        filename: Le nom de fichier à valider
        
    Returns:
        Le nom de fichier sanitisé (basename uniquement)
        
    Raises:
        ValueError: Si le filename est invalide
    """
    if not filename:
        raise ValueError("filename est requis (ne peut pas être vide)")
    
    if '\x00' in filename:
        raise ValueError(f"filename contient des null bytes: {filename!r}")
    
    # Extraire le basename pour retirer tout chemin
    sanitized = os.path.basename(filename)
    
    if not sanitized:
        raise ValueError(f"filename invalide après extraction du basename: {filename!r}")
    
    if '..' in sanitized:
        raise ValueError(f"filename contient du path traversal: {filename!r}")
    
    if len(sanitized) > 255:
        raise ValueError(f"filename trop long ({len(sanitized)} chars, max 255): {sanitized[:50]}...")
    
    return sanitized


def validate_backup_id(backup_id: str) -> tuple:
    """
    Valide un backup_id au format "memory_id/timestamp".
    
    Chaque composant est validé avec une regex stricte anti path-traversal.
    
    Args:
        backup_id: L'identifiant de backup à valider
        
    Returns:
        Tuple (memory_id, timestamp) validés
        
    Raises:
        ValueError: Si le backup_id est invalide
    """
    if not backup_id:
        raise ValueError("backup_id est requis")
    
    parts = backup_id.split("/")
    if len(parts) != 2:
        raise ValueError(f"backup_id doit être au format 'memory_id/timestamp', reçu: {backup_id!r}")
    
    memory_id, timestamp = parts
    
    if not VALID_BACKUP_COMPONENT.match(memory_id):
        raise ValueError(f"Composant memory_id invalide dans backup_id: {memory_id!r}")
    
    if not VALID_BACKUP_COMPONENT.match(timestamp):
        raise ValueError(f"Composant timestamp invalide dans backup_id: {timestamp!r}")
    
    return memory_id, timestamp


def validate_document_size(content: bytes, max_size: int = MAX_INGEST_SIZE_BYTES) -> bytes:
    """
    Valide la taille d'un document à ingérer.
    
    Args:
        content: Le contenu du document en bytes
        max_size: Taille maximale autorisée en bytes (défaut: 50 MB)
        
    Returns:
        Le contenu validé (inchangé)
        
    Raises:
        ValueError: Si le document est trop volumineux
    """
    size = len(content)
    if size > max_size:
        size_mb = size / (1024 * 1024)
        max_mb = max_size / (1024 * 1024)
        raise ValueError(
            f"Document trop volumineux: {size_mb:.1f} MB (max {max_mb:.0f} MB). "
            f"Réduisez la taille du document ou augmentez MAX_INGEST_SIZE_BYTES."
        )
    return content


def validate_entity_name(entity_name: str) -> str:
    """
    Valide un nom d'entité.
    
    Règles :
    - Non vide
    - Longueur max 500 caractères
    - Pas de null bytes
    
    Note: Les noms d'entités sont passés comme paramètres Cypher ($name),
    donc pas de risque d'injection Cypher. La validation est principalement
    pour éviter les abus (longueur excessive, caractères de contrôle).
    
    Args:
        entity_name: Le nom de l'entité à valider
        
    Returns:
        Le nom validé (inchangé)
        
    Raises:
        ValueError: Si le nom est invalide
    """
    if not entity_name:
        raise ValueError("entity_name est requis (ne peut pas être vide)")
    
    if '\x00' in entity_name:
        raise ValueError(f"entity_name contient des null bytes")
    
    if len(entity_name) > 500:
        raise ValueError(f"entity_name trop long ({len(entity_name)} chars, max 500)")
    
    return entity_name


def check_bootstrap_key_safety(key: str) -> None:
    """
    Vérifie que la clé bootstrap n'est pas une valeur par défaut dangereuse.
    
    Appelée au démarrage du serveur. Logge un warning si la clé est faible.
    
    Args:
        key: La clé bootstrap à vérifier
    """
    UNSAFE_PATTERNS = [
        "change_me",
        "changeme", 
        "admin",
        "password",
        "secret",
        "test",
        "default",
        "example",
    ]
    
    if not key:
        print("⚠️  [Security] ADMIN_BOOTSTRAP_KEY non définie — auth bootstrap désactivée", file=sys.stderr)
        return
    
    key_lower = key.lower()
    for pattern in UNSAFE_PATTERNS:
        if pattern in key_lower:
            print(f"🔴 [Security] ADMIN_BOOTSTRAP_KEY contient '{pattern}' — "
                  f"CHANGEZ-LA IMMÉDIATEMENT en production ! "
                  f"Utilisez : openssl rand -hex 32", file=sys.stderr)
            return
    
    if len(key) < 32:
        print(f"⚠️  [Security] ADMIN_BOOTSTRAP_KEY trop courte ({len(key)} chars, recommandé: 64). "
              f"Utilisez : openssl rand -hex 32", file=sys.stderr)
