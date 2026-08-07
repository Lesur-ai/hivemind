# -*- coding: utf-8 -*-
"""
P7-7 (#123) — embedded-long topology / runtime integration guards (ADR-0019).

P7-7 is mostly a consolidation + gap-closure task: the compose service-set,
no-host-ports, source-inventory, and long-isolation ACs are ALREADY green
(``test_release_rebrand_lint.py``, ``test_source_inventory_graph_memory.py``,
``test_long_isolation.py``, ``test_long_runtime_isolation.py``). This file adds
only the *net-new* P7-7-owned facts that no single existing test nails:

- the P7-3 hivemind↔embedded-long wiring in compose (env vars + secret volume +
  fail-closed ``depends_on graph-memory: service_healthy``) — locked against
  silent removal;
- ``.env.example`` documents the embedded long + its datastores (Neo4j/Qdrant);
- the embedded sentinel ``__embedded__`` is never an ASSIGNED value in compose /
  ``.env.example`` (local-only secret contract, ADR-0012).

Pure stdlib; reuses the compose parser from ``test_release_rebrand_lint.py``
(no re-implementation — a copy would be flagged).

Hardening (Codex committed-diff review, fix-round 1): the topology guards are
STRUCTURED, not bare substring greps, and each hardened guard carries an inline
RED-proof test that mutates a COPY of the real compose/.env.example text (the
real files are never touched):

- ``depends_on`` guard — parses the hivemind service block → its ``depends_on``
  sub-block → the ``graph-memory`` entry, and requires
  ``condition: service_healthy`` INSIDE that entry. Catches the mutation where
  the healthy-condition sits on ANOTHER dependency while
  ``http://graph-memory:8002`` in LONG_EMBEDDED_URL keeps the old
  ``"graph-memory:"`` substring alive.
- env guard — each ``LONG_EMBEDDED_*`` var must be a real entry of the hivemind
  ``environment:`` block (``- VAR=…`` list form or ``VAR: …`` mapping form),
  comment lines excluded. Catches the mutation where the binding is deleted but
  the var name survives in a comment / prose (or, for ``LONG_EMBEDDED_TOKEN``,
  as a substring of the ``LONG_EMBEDDED_TOKEN_FILE`` entry).
- sentinel guard — scans every non-comment config line of compose AND
  ``.env.example``: the reserved ``__embedded__`` sentinel must never appear as
  a configured value in ANY form. Catches the quoted (``'__embedded__'`` /
  ``"__embedded__"``), YAML-mapping (``LONG_EMBEDDED_TOKEN: __embedded__``) and
  compose interpolation-default (``${LONG_EMBEDDED_TOKEN:-__embedded__}``)
  shapes that escaped the old literal ``=__embedded__`` check.

Scope note: the operator-doc drift lints (``LONG_BACKEND_IMAGE`` /
``operator-supplied image`` / ``provision separately`` in ``docs/DEPLOYMENT.md``
etc.) are DEFERRED to **P7-6**, which both rewrites the drifted doc sections AND
adds the enforcing lint — guard and fix ship together (``docs/DEPLOYMENT.md``
currently still carries the P6 opt-in drift, so a lint here would be RED until
P7-6 lands). P7-7 therefore guards only surfaces that are already clean:
the compose↔embedded wiring, the ``.env.example`` datastore documentation, and
the sentinel-not-assigned contract. The two MINOR gate-sequence deferrals owned
by P7-7 land next to the gates they harden: ADR-0019 boundary-clause pins now
consolidated in ``test_architecture_contracts.py``, vendored-tree
venv/datastore-volume hygiene in ``test_source_inventory_graph_memory.py``.

Out of scope (other Wave-3 children): release-smoke disabled-state (P7-5);
backup-at-rest secret masking + GM ``document_delete`` write-gate (P7-8);
operator-doc drift lints + rewrite (P7-6).
"""

from __future__ import annotations

import re

import pytest

from tests.test_release_rebrand_lint import (
    REPO_ROOT,
    _read,
    _iter_top_level_services,
)


def _compose() -> str:
    return _read(REPO_ROOT / "docker-compose.yml")


def _hivemind_block(compose_text: str) -> str:
    return _service_block(compose_text, "hivemind")


def _service_block(compose_text: str, service: str) -> str:
    for name, block in _iter_top_level_services(compose_text):
        if name == service:
            return block
    raise AssertionError(f"compose has no top-level {service!r} service")


def _subsection(block: str, key: str) -> str:
    """Return the ``key:`` sub-block of a YAML block: the first line whose
    stripped content is exactly ``key:`` (or ``key: <inline>``) plus every
    following line indented strictly deeper. Empty string if absent.

    Same minimal stdlib-only compose-parsing approach as
    ``_iter_top_level_services`` (imported from
    ``tests/test_release_rebrand_lint.py``), applied one nesting level down —
    deliberately NOT a YAML library (the lint must stay dependency-free) and
    deliberately strict: shorthand list forms (``depends_on: [graph-memory]`` /
    ``- graph-memory``) yield no sub-block, so a guard looking for a
    ``condition:`` inside them fails closed.
    """
    grabbed: list[str] = []
    key_indent: int | None = None
    for line in block.splitlines():
        stripped = line.strip()
        if key_indent is None:
            if stripped == f"{key}:" or stripped.startswith(f"{key}: "):
                key_indent = len(line) - len(line.lstrip(" "))
                grabbed.append(line)
            continue
        indent = len(line) - len(line.lstrip(" "))
        if stripped and indent <= key_indent:
            break
        grabbed.append(line)
    return "\n".join(grabbed)


def _mutate(text: str, old: str, new: str) -> str:
    """Return a mutated COPY of ``text`` for RED-proof tests; asserts the
    anchor exists so a compose refactor cannot silently turn a mutation
    self-test into a no-op."""
    assert old in text, f"mutation anchor missing from compose: {old!r}"
    return text.replace(old, new)


# --------------------------------------------------------------------------- #
# Hardened guard predicates (pure functions of the compose/.env text so the    #
# RED-proof tests below can exercise them against mutated copies)              #
# --------------------------------------------------------------------------- #


def _assert_hivemind_embedded_env_wiring(compose_text: str) -> None:
    """The hivemind service must bind the embedded long runtime via REAL
    ``environment:`` entries (P7-3 auto-bind) and mount the local-only secret
    volume as a REAL ``volumes:`` entry."""
    block = _hivemind_block(compose_text)

    env_block = _subsection(block, "environment")
    assert env_block, "hivemind service must declare an environment: section"
    for var in ("LONG_EMBEDDED_URL", "LONG_EMBEDDED_TOKEN", "LONG_EMBEDDED_TOKEN_FILE"):
        # list form `- VAR=…` or mapping form `VAR: …`; comment lines excluded.
        entry_re = re.compile(rf"^\s*-?\s*{var}\s*[=:]")
        entries = [
            ln
            for ln in env_block.splitlines()
            if not ln.strip().startswith("#") and entry_re.match(ln)
        ]
        assert entries, (
            f"hivemind environment: block must wire {var} as a real entry "
            f"(P7-3 auto-bind) — a comment or prose mention does not count"
        )

    vol_block = _subsection(block, "volumes")
    assert vol_block, "hivemind service must declare a volumes: section"
    assert re.search(r"^\s*-\s*hivemind_secrets:/data/secrets\s*$", vol_block, re.M), (
        "hivemind volumes: must mount hivemind_secrets at /data/secrets "
        "(P7-3 local-only embedded secret store)"
    )


def _assert_hivemind_depends_on_graph_memory_healthy(compose_text: str) -> None:
    """Fail-closed completeness (ADR-0019 §Consequences): the ``graph-memory``
    entry INSIDE hivemind's ``depends_on`` must itself carry
    ``condition: service_healthy`` — a healthy-condition on another dependency
    must not satisfy the guard."""
    block = _hivemind_block(compose_text)
    dep_block = _subsection(block, "depends_on")
    assert dep_block, "hivemind service must declare a depends_on: section"
    gm_entry = _subsection(dep_block, "graph-memory")
    assert gm_entry, (
        "hivemind depends_on must list graph-memory (mapping form, so a "
        "condition can be attached)"
    )
    assert re.search(r"^\s*condition:\s*service_healthy\s*$", gm_entry, re.M), (
        "hivemind depends_on graph-memory must set condition: service_healthy "
        "(ADR-0019 fail-closed completeness)"
    )


def _list_values(section: str) -> list[str]:
    return [
        line.strip()[2:].strip().strip('"\'')
        for line in section.splitlines()[1:]
        if line.strip().startswith("- ") and not line.strip().startswith("- #")
    ]


def _assert_embedded_secret_initializer(compose_text: str) -> None:
    """Pin the least-privilege one-shot and the main startup dependency."""
    init_block = _service_block(compose_text, "hivemind-secrets-init")
    main_block = _hivemind_block(compose_text)

    for block in (init_block, main_block):
        assert re.search(r"^\s*<<:\s*\*hivemind-image\s*$", block, re.M)
    anchor = compose_text.split("\nservices:", 1)[0]
    for required in (
        "x-hivemind-image: &hivemind-image",
        "  build:\n    context: .",
        "  image: hivemind:latest",
        "  pull_policy: build",
    ):
        assert required in anchor

    for scalar in (
        'user: "0:0"',
        'command: ["python", "/app/scripts/init_embedded_secret_volume.py"]',
        "network_mode: none",
        "read_only: true",
        'restart: "no"',
    ):
        assert re.search(rf"^\s*{re.escape(scalar)}\s*$", init_block, re.M)

    for forbidden in ("environment", "env_file", "ports", "networks", "depends_on"):
        assert not _subsection(init_block, forbidden), (
            f"secret initializer must not declare {forbidden}"
        )
    assert _list_values(_subsection(init_block, "security_opt")) == [
        "no-new-privileges:true"
    ]
    assert _list_values(_subsection(init_block, "cap_drop")) == ["ALL"]
    assert _list_values(_subsection(init_block, "cap_add")) == ["CHOWN"]
    assert _list_values(_subsection(init_block, "volumes")) == [
        "hivemind_secrets:/data/secrets"
    ]
    health = _subsection(init_block, "healthcheck")
    assert re.search(r"^\s*disable:\s*true\s*$", health, re.M)

    env = _subsection(main_block, "environment")
    assert re.search(
        r"^\s*-\s*LONG_EMBEDDED_TOKEN_FILE=/data/secrets/long_embedded_token\s*$",
        env,
        re.M,
    )
    assert "LONG_EMBEDDED_TOKEN_FILE=${" not in env
    dependency = _subsection(_subsection(main_block, "depends_on"), "hivemind-secrets-init")
    assert re.search(
        r"^\s*condition:\s*service_completed_successfully\s*$",
        dependency,
        re.M,
    )


def _sentinel_value_lines(text: str) -> list[str]:
    """Return the config lines where the reserved ``__embedded__`` sentinel
    appears as a value.

    Full-line comments are skipped and inline trailing comments (`` #…``) are
    stripped, so prose documenting the reserved value (``.env.example``:
    "Must NOT equal '__embedded__'") stays legal. ANY remaining occurrence is
    flagged — deliberately broader than a ``KEY=`` regex so every assignment
    shape is covered: ``=__embedded__``, ``='__embedded__'``,
    ``="__embedded__"``, YAML mapping ``KEY: __embedded__`` (quoted or not),
    list entries ``- KEY=__embedded__``, and compose interpolation defaults
    ``${VAR:-__embedded__}``; and for ANY key, not only LONG_EMBEDDED_TOKEN
    (the sentinel is a persisted marker, never a legal live config value,
    ADR-0012 / P7-3).
    """
    bad: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        code = raw.split(" #", 1)[0]
        if "__embedded__" in code:
            bad.append(stripped)
    return bad


# --------------------------------------------------------------------------- #
# Guards on the REAL files                                                     #
# --------------------------------------------------------------------------- #


def test_compose_hivemind_binds_embedded_long_env():
    _assert_hivemind_embedded_env_wiring(_compose())


def test_compose_hivemind_depends_on_graph_memory_healthy():
    _assert_hivemind_depends_on_graph_memory_healthy(_compose())


def test_graph_memory_healthchecks_use_only_runtime_python():
    """The slim image contains Python but deliberately installs no curl."""
    compose_health = _subsection(
        _service_block(_compose(), "graph-memory"), "healthcheck"
    )
    dockerfile = _read(REPO_ROOT / "services" / "graph-memory" / "Dockerfile")
    for healthcheck in (compose_health, dockerfile):
        assert "urllib.request" in healthcheck
        assert "/ready" in healthcheck
        assert "curl" not in healthcheck

    mutated = compose_health.replace("python", "curl", 1)
    assert "urllib.request" in mutated and "curl" in mutated


def test_compose_embedded_secret_initializer_is_least_privilege():
    _assert_embedded_secret_initializer(_compose())


def test_env_example_documents_embedded_long_and_datastores():
    env = _read(REPO_ROOT / ".env.example")
    for var in (
        "LONG_EMBEDDED_URL",
        "LONG_EMBEDDED_TOKEN",
        "LONG_EMBEDDED_TOKEN_FILE",
        "NEO4J_PASSWORD",
        "QDRANT_",
    ):
        assert var in env, f".env.example must document {var} (embedded long + datastores)"


def test_embedded_sentinel_never_assigned_in_compose_or_env():
    # The sentinel must never be an ASSIGNED value (it is a persisted marker,
    # not a live bearer) — in any form: bare, quoted, YAML mapping, or
    # interpolation default. Prose mentions in comments stay legal
    # (.env.example documents the reserved value: "Must NOT equal
    # '__embedded__'").
    for rel in ("docker-compose.yml", ".env.example"):
        bad = _sentinel_value_lines(_read(REPO_ROOT / rel))
        assert not bad, (
            f"{rel} pre-fills the reserved embedded sentinel __embedded__ — "
            f"it must never be a configured value (ADR-0012 / P7-3): {bad}"
        )


# --------------------------------------------------------------------------- #
# RED-proofs — each hardened guard rejects the realistic mutation that the     #
# pre-hardening substring checks accepted (mutations run on COPIES of the      #
# real files; the files themselves are never modified)                         #
# --------------------------------------------------------------------------- #

_HIVEMIND_GRAPH_DEPENDENCY = (
    "      graph-memory:\n" "        condition: service_healthy"
)


def test_mutation_red_healthy_condition_on_other_dependency():
    """RED-proof for the depends_on guard: move the healthy-condition to a
    different dependency. The OLD substring check ('graph-memory:' anywhere in
    the hivemind block + 'condition: service_healthy' anywhere in it) still
    PASSES — LONG_EMBEDDED_URL's default http://graph-memory:8002 supplies the
    'graph-memory:' substring — but the structured guard goes RED."""
    mutated = _mutate(
        _compose(),
        _HIVEMIND_GRAPH_DEPENDENCY,
        "      minio:\n" "        condition: service_healthy",
    )
    block = _hivemind_block(mutated)
    # The pre-hardening predicate is demonstrably still satisfied…
    assert "graph-memory:" in block and "condition: service_healthy" in block
    # …while the hardened guard rejects the mutation.
    with pytest.raises(AssertionError):
        _assert_hivemind_depends_on_graph_memory_healthy(mutated)


def test_mutation_red_condition_weakened_to_service_started():
    """RED-proof for the depends_on guard: keep the graph-memory dependency but
    weaken the condition — fail-closed completeness (ADR-0019) requires
    service_healthy, not service_started."""
    mutated = _mutate(
        _compose(),
        _HIVEMIND_GRAPH_DEPENDENCY,
        "      graph-memory:\n" "        condition: service_started",
    )
    with pytest.raises(AssertionError):
        _assert_hivemind_depends_on_graph_memory_healthy(mutated)


def test_mutation_red_env_binding_survives_only_as_comment():
    """RED-proof for the env guard: delete the LONG_EMBEDDED_TOKEN entry and
    leave only a comment naming it. The OLD substring check ('LONG_EMBEDDED_TOKEN'
    in the hivemind block) still PASSES — via the comment AND via the
    LONG_EMBEDDED_TOKEN_FILE entry, of which the token var name is a substring —
    but the hardened guard requires a real environment: entry and goes RED."""
    mutated = _mutate(
        _compose(),
        "      - LONG_EMBEDDED_TOKEN=${LONG_EMBEDDED_TOKEN:-}",
        "      # LONG_EMBEDDED_TOKEN: intentionally not wired (mutation fixture)",
    )
    block = _hivemind_block(mutated)
    # The pre-hardening predicate is demonstrably still satisfied…
    assert "LONG_EMBEDDED_TOKEN" in block
    # …while the hardened guard rejects the mutation.
    with pytest.raises(AssertionError):
        _assert_hivemind_embedded_env_wiring(mutated)


@pytest.mark.parametrize(
    "old,new",
    [
        ("      - CHOWN", "      - DAC_OVERRIDE"),
        ("    network_mode: none", "    networks:\n      - hivemind-network"),
        ("    read_only: true", "    read_only: false"),
        (
            "    volumes:\n      - hivemind_secrets:/data/secrets\n    restart:",
            "    volumes:\n"
            "      - hivemind_secrets:/data/secrets\n"
            "      - minio_data:/unexpected\n"
            "    restart:",
        ),
        (
            "      - LONG_EMBEDDED_TOKEN_FILE=/data/secrets/long_embedded_token",
            "      - LONG_EMBEDDED_TOKEN_FILE=${LONG_EMBEDDED_TOKEN_FILE:-/data/secrets/long_embedded_token}",
        ),
        ("        condition: service_completed_successfully", "        condition: service_started"),
    ],
)
def test_mutation_red_secret_initializer_hardening(old: str, new: str) -> None:
    mutated = _mutate(_compose(), old, new)
    with pytest.raises(AssertionError):
        _assert_embedded_secret_initializer(mutated)


def test_mutation_red_initializer_receives_env_file() -> None:
    mutated = _mutate(
        _compose(),
        "    network_mode: none",
        "    env_file: .env\n    network_mode: none",
    )
    with pytest.raises(AssertionError):
        _assert_embedded_secret_initializer(mutated)


@pytest.mark.parametrize(
    "assignment",
    [
        # base form (already caught pre-hardening — kept as regression anchor)
        "LONG_EMBEDDED_TOKEN=__embedded__",
        # forms that ESCAPED the old literal '=__embedded__' check:
        "LONG_EMBEDDED_TOKEN='__embedded__'",
        'LONG_EMBEDDED_TOKEN="__embedded__"',
        "LONG_EMBEDDED_TOKEN: __embedded__",
        'LONG_EMBEDDED_TOKEN: "__embedded__"',
        "- LONG_EMBEDDED_TOKEN=${LONG_EMBEDDED_TOKEN:-__embedded__}",
    ],
)
def test_mutation_red_sentinel_prefilled_in_any_form(assignment):
    """RED-proof for the sentinel guard: every quoted / YAML-mapping /
    interpolation-default pre-fill of the reserved sentinel is caught
    (mutating a COPY of the real .env.example text)."""
    mutated = _read(REPO_ROOT / ".env.example") + "\n" + assignment + "\n"
    assert _sentinel_value_lines(mutated) == [assignment.strip()]


def test_sentinel_prose_comment_stays_legal():
    """Anti-over-blocking check: a comment documenting the reserved sentinel
    (as .env.example legitimately does) must NOT be flagged."""
    fixture = (
        "# The reserved sentinel is '__embedded__' — never assign it.\n"
        "LONG_EMBEDDED_TOKEN=\n"
    )
    assert _sentinel_value_lines(fixture) == []


def test_public_runtime_images_are_immutable_multiarch_inputs() -> None:
    """Every registry-supplied runtime/build image is tag+digest pinned.

    Local ``hivemind:*`` images are deliberately excluded: Compose builds them
    from the pinned Dockerfiles in this repository.
    """
    digest = re.compile(r"@sha256:[0-9a-f]{64}(?:\s|$)")
    compose = _compose()
    external_images = []
    for raw in compose.splitlines():
        line = raw.strip()
        if not line.startswith("image:"):
            continue
        image = line.split(":", 1)[1].strip()
        if image.startswith("hivemind:") or image.startswith("hivemind-graph-memory:"):
            continue
        external_images.append(image)
    assert external_images
    assert all(digest.search(image) for image in external_images), external_images

    dockerfiles = (
        REPO_ROOT / "Dockerfile",
        REPO_ROOT / "waf" / "Dockerfile",
        REPO_ROOT / "services" / "graph-memory" / "Dockerfile",
    )
    for path in dockerfiles:
        from_lines = [
            line.strip()
            for line in _read(path).splitlines()
            if line.lstrip().startswith("FROM ")
        ]
        assert from_lines, f"{path} has no FROM input"
        assert all(digest.search(line) for line in from_lines), (
            f"{path} has a mutable base image: {from_lines}"
        )
