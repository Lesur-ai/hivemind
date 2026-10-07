# -*- coding: utf-8 -*-
"""
derive_memory_id : déterministe, GM-regex-safe, anti-collision.

RED sans src/live_mem/core/memory_id.py. Le golden littéral fige l'algorithme :
tout changement de préfixe/longueur/hash le casse (anti-dérive).
"""

from __future__ import annotations

import re

import pytest

from live_mem.core import memory_id
from live_mem.core.memory_id import derive_memory_id

# Byte-identique à GM ``VALID_MEMORY_ID`` (services/graph-memory/src/mcp_memory/
# core/validators.py:25) ET à Hivemind ``SPACE_ID_REGEX`` (src/live_mem/core/
# space.py:33). Inline plutôt qu'importé : mcp_memory.core traîne neo4j (absent
# du venv Hivemind). Un import lourd casserait la collecte.
VALID_MEMORY_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def test_derive_is_deterministic_and_frozen() -> None:
    # Golden gelé : si ceci change, l'anti-dérive a détecté une modif d'algo.
    assert derive_memory_id("example-project") == "hm-example-project-7357820d4b2f1bba"
    # Déterminisme strict : deux appels identiques.
    assert derive_memory_id("space-a") == derive_memory_id("space-a")
    assert derive_memory_id("space-a") == "hm-space-a-a70172e8ecf5336e"


@pytest.mark.parametrize(
    ("constant", "mutated"),
    (("_PREFIX", "other-"), ("_HASH_BYTES", 7), ("_BODY_MAX", 8)),
)
def test_neutral_golden_fixture_still_detects_algorithm_drift(monkeypatch, constant, mutated):
    test_derive_is_deterministic_and_frozen()
    monkeypatch.setattr(memory_id, constant, mutated)
    with pytest.raises(AssertionError):
        test_derive_is_deterministic_and_frozen()


@pytest.mark.parametrize(
    "space_id",
    [
        "a",
        "space-a",
        "example-project",
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


@pytest.mark.parametrize("space_id", ["a", "A" * 64, "UPPER_and-lower_123", "___"])
def test_derived_names_are_reserved_without_changing_the_algorithm(space_id):
    error = memory_id.reserved_space_error(derive_memory_id(space_id))
    assert error["recovery_required"] is True


@pytest.mark.parametrize("space_id", [
    "ordinary", "hm-team", "hm-team-0123", "hm-team-0123456789abcdeg",
    "mid_team", "mid_" + "a" * 39, "mid_" + "a" * 41,
    "mid_" + "g" * 40, "mid_" + "A" * 40,
])
def test_ordinary_names_are_not_reserved(space_id):
    assert memory_id.reserved_space_error(space_id) is None


@pytest.mark.parametrize("digest", ["0" * 40, "0123456789abcdef" * 2 + "01234567"])
def test_exact_mid_archive_namespace_is_reserved(digest):
    assert memory_id.reserved_space_error("mid_" + digest)["recovery_required"] is True
