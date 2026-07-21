# -*- coding: utf-8 -*-
"""
P7-3 — derive_memory_id : déterministe, GM-regex-safe, anti-collision.

RED sans src/live_mem/core/memory_id.py. Le golden littéral fige l'algorithme :
tout changement de préfixe/longueur/hash le casse (anti-dérive).
"""

from __future__ import annotations

import re

import pytest

from live_mem.core.memory_id import derive_memory_id

# Byte-identique à GM ``VALID_MEMORY_ID`` (services/graph-memory/src/mcp_memory/
# core/validators.py:25) ET à Hivemind ``SPACE_ID_REGEX`` (src/live_mem/core/
# space.py:33). Inline plutôt qu'importé : mcp_memory.core traîne neo4j (absent
# du venv Hivemind). Un import lourd casserait la collecte.
VALID_MEMORY_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def test_derive_is_deterministic_and_frozen() -> None:
    # Golden gelé : si ceci change, l'anti-dérive a détecté une modif d'algo.
    assert derive_memory_id("lesur-ai-hivemind") == "hm-lesur-ai-hivemind-a943bfbdb0f5f73a"
    # Déterminisme strict : deux appels identiques.
    assert derive_memory_id("space-a") == derive_memory_id("space-a")
    assert derive_memory_id("space-a") == "hm-space-a-a70172e8ecf5336e"


@pytest.mark.parametrize(
    "space_id",
    [
        "a",
        "space-a",
        "lesur-ai-hivemind",
        "A" * 64,  # space_id maximal
        "weird/../..\x00chars ok?",  # chars illégaux + traversal + null + espace
        "UPPER_and-lower_123",
    ],
)
def test_derive_is_gm_regex_safe(space_id: str) -> None:
    mid = derive_memory_id(space_id)
    assert VALID_MEMORY_ID.match(mid), f"{mid!r} viole VALID_MEMORY_ID"
    assert len(mid) <= 64
    assert ".." not in mid and "\x00" not in mid


def test_derive_distinct_after_sanitization() -> None:
    # Deux space_id BRUTS distincts qui se sanitizent au même corps doivent
    # garder des memory_id distincts (hash sur le brut, pas sur le corps).
    a = derive_memory_id("foo/bar")
    b = derive_memory_id("foo.bar")
    assert a != b
    # Même corps sanitizé "foo-bar", suffixes hash différents.
    assert a.rsplit("-", 1)[0] == b.rsplit("-", 1)[0]
    assert a.rsplit("-", 1)[1] != b.rsplit("-", 1)[1]


def test_derive_empty_raises() -> None:
    with pytest.raises(ValueError):
        derive_memory_id("")
