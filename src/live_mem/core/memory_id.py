# -*- coding: utf-8 -*-
"""
Déterministe ``space_id`` → ``memory_id`` pour le runtime long embarqué (P7-3).

Le tier ``long`` embarqué (ADR-0019) lie automatiquement chaque space Hivemind à
une mémoire Graph Memory interne. Le ``memory_id`` cible est **dérivé** du
``space_id`` — jamais fourni par l'opérateur — de façon :

- **déterministe** : le MÊME ``space_id`` produit TOUJOURS le même ``memory_id``
  (aucune horloge, aucun RNG, aucun ``hash()`` built-in salé par process). Un
  test anti-dérive fige un golden littéral ; changer préfixe/algorithme/longueur
  le casse en RED.
- **GM-regex-safe** : le résultat satisfait ``VALID_MEMORY_ID`` de Graph Memory
  (``^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$`` — 1-64 chars, commence par alphanum),
  byte-identique au ``SPACE_ID_REGEX`` Hivemind. Pas de ``..`` (path traversal),
  pas de null byte : le sanitizer ne garde que ``[a-zA-Z0-9_-]``.
- **anti-collision** : un suffixe blake2b 64-bit sur le ``space_id`` BRUT (pas
  sur le corps sanitizé) garantit que deux ``space_id`` distincts qui se
  sanitizent au même corps gardent des suffixes distincts.

Module PUR (stdlib only). Volontairement isolé : AUCUN module du chemin de
commit ne l'importe (ADR-0010 ; verrouillé par test_long_isolation).
"""

from __future__ import annotations

import hashlib
import re

# Interdit tout ce qui n'est pas dans la charset GM/Hivemind. Remplacé par '-'.
_ILLEGAL = re.compile(r"[^a-zA-Z0-9_-]")

_PREFIX = "hm-"  # 3 chars ; commence par 'h' (alphanum) → leading-char rule OK
_HASH_HEX = 16  # 16 hex = 64 bits (blake2b digest_size=8)
_HASH_BYTES = 8
_MAX = 64  # cap dur GM (VALID_MEMORY_ID : leading + {0,63})
# Budget corps = 64 - len("hm-") - len("-") - 16 = 44
_BODY_MAX = _MAX - len(_PREFIX) - 1 - _HASH_HEX


def derive_memory_id(space_id: str) -> str:
    """Dérive un ``memory_id`` GM-valide et déterministe depuis un ``space_id``.

    Args:
        space_id: identifiant du space Hivemind (non vide).

    Returns:
        ``"hm-<sanitized[:44]>-<blake2b64hex>"`` (≤ 64 chars, GM-regex-safe).

    Raises:
        ValueError: si ``space_id`` est vide.
    """
    if not space_id:
        raise ValueError("space_id is required to derive a memory_id")
    # Hash sur le space_id BRUT (anti-collision post-sanitization).
    digest = hashlib.blake2b(
        space_id.encode("utf-8"), digest_size=_HASH_BYTES
    ).hexdigest()
    sanitized = _ILLEGAL.sub("-", space_id)[:_BODY_MAX]
    return f"{_PREFIX}{sanitized}-{digest}"
