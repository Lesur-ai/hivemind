# -*- coding: utf-8 -*-
"""Release-gate provenance lint (ADR-0018).

Locks the structural identity reset away from the inherited Live Memory
`2.5.x` line:

- ``VERSION`` is either the pre-decision placeholder ``0.0.0+unreleased``
  (PEP 440 local-version segment — explicitly "no commitment to public
  version") or, once the user has decided the starting SemVer at release-cut
  time (``1.0.0-beta.1``, 2026-07-07), a public SemVer
  ``MAJOR.MINOR.PATCH`` with an optional pre-release suffix. Either way it
  MUST NOT start with ``2.5.`` and MUST NOT equal ``2.5.2`` (the inherited
  Live Memory line), and it MUST be PEP 440-valid
  (``packaging.version.parse`` must not raise) so ``uv sync --dev`` and
  ``setuptools`` accept it.
- ``CHANGELOG.md`` head is renamed to ``Changelog - Hivemind`` (the
  ``Changelog - Live Memory`` title is gone from the first 30 lines).
- ``CHANGELOG.md`` carries an ``Inherited Live Memory history
  (provenance)`` section header somewhere in the body so the inherited
  2.5.x entries are wrapped under a clearly identified provenance section.
- ``docs/WORKFLOW_GIT_EPIC.md`` no longer carries the
  ``No Hivemind release train is active`` prelude.
- ``docs/WORKFLOW_GIT_EPIC.md`` embeds the seven ADR-0018 HTML-comment
  sentinels at the top of their sub-sections so the release-gate lint
  detects structural coverage deterministically, including a Docker
  ``image-build`` sentinel enforcing the Docker-image gate alongside
  pytest / tool-surface / docs / non-claims / provenance / smoke.

Stdlib-only, offline. Resolves the repo root from ``__file__``.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# VERSION
# ---------------------------------------------------------------------------


def test_version_file_is_placeholder_or_decided_public_semver() -> None:
    path = _REPO_ROOT / "VERSION"
    assert path.exists(), "VERSION file must exist at repo root"
    value = path.read_text(encoding="utf-8").strip()

    # Reject the inherited Live Memory line.
    assert value != "2.5.2", (
        "VERSION still equals '2.5.2' (inherited Live Memory). ADR-0018 "
        "requires the Hivemind public release to reset the version line."
    )
    assert not value.startswith("2.5."), (
        f"VERSION starts with '2.5.' ({value!r}) which continues the "
        "inherited Live Memory series. ADR-0018 forbids this."
    )

    # Accepted values (ADR-0018):
    #   1. the pre-decision placeholder `0.0.0+unreleased` (PEP 440
    #      local-version segment, optionally with an extra `.<token>`
    #      suffix) — "no commitment to public version" before the user
    #      decides the starting SemVer;
    #   2. a decided public SemVer `MAJOR.MINOR.PATCH` with an optional
    #      `-<pre-release>` suffix (the user decided `1.0.0-beta.1` at
    #      release-cut time, P7-9 / 2026-07-07). Pre-1.0 and pre-release
    #      versions signal an unstable public contract; the release notes
    #      must say so explicitly.
    # The earlier `0.0.0-unreleased` form stays REJECTED because
    # setuptools / packaging.version refuse it, which breaks
    # `uv sync --dev` in CI.
    placeholder = re.fullmatch(r"0\.0\.0\+unreleased(\.[A-Za-z0-9.\-]+)?", value)
    # Pre-release grammar per SemVer 2.0.0 §9: dot-separated identifiers,
    # numeric identifiers without leading zeros — so `1.0.0-01` or
    # `1.0.0-beta.01` fail HERE at lint time instead of surfacing later as a
    # normalization surprise (packaging would silently accept both).
    _ident = r"(0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    public_semver = re.fullmatch(
        r"(?!0\.0\.0)(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        rf"(-{_ident}(\.{_ident})*)?",
        value,
    )
    assert placeholder or public_semver, (
        f"VERSION = {value!r}; ADR-0018 accepts either the exact "
        "'0.0.0+unreleased' placeholder (optionally with an extra "
        "'.<token>' suffix) or a decided public SemVer "
        "'MAJOR.MINOR.PATCH[-prerelease]'."
    )

    # Positive PEP 440 validity assertion: any future placeholder MUST be
    # accepted by `packaging.version.parse` so `uv sync --dev`,
    # `setuptools`, `pip` and downstream resolvers do not fail at install
    # time. `packaging` is already a transitive dep of `pip` / `setuptools`
    # and is bundled with `uv`'s resolver, so the import is safe in CI.
    try:
        from packaging.version import Version  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - defensive
        import pytest as _pytest

        _pytest.skip("packaging not installed; PEP 440 validity check skipped")
    Version(value)  # raises packaging.version.InvalidVersion on bad input


# ---------------------------------------------------------------------------
# CHANGELOG.md
# ---------------------------------------------------------------------------


def _changelog_text() -> str:
    path = _REPO_ROOT / "CHANGELOG.md"
    assert path.exists(), "CHANGELOG.md must exist at repo root"
    return path.read_text(encoding="utf-8")


def test_changelog_title_renamed_to_hivemind() -> None:
    text = _changelog_text()
    head = "\n".join(text.splitlines()[:30])
    assert "Changelog — Live Memory" not in head and "Changelog - Live Memory" not in head, (
        "CHANGELOG.md head still carries the inherited 'Changelog — Live "
        "Memory' title in its first 30 lines. ADR-0018 requires the file "
        "to be renamed to 'Changelog — Hivemind'."
    )
    assert "Changelog — Hivemind" in head or "Changelog - Hivemind" in head, (
        "CHANGELOG.md head MUST carry the 'Changelog — Hivemind' title "
        "(em dash or ASCII hyphen) within the first 30 lines."
    )


def test_changelog_has_inherited_live_memory_provenance_section() -> None:
    text = _changelog_text()
    # Case-insensitive contains-check on the provenance heading.
    assert re.search(
        r"^##\s+Inherited\s+Live\s+Memory\s+history\s+\(provenance\)\s*$",
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    ), (
        "CHANGELOG.md MUST carry a '## Inherited Live Memory history "
        "(provenance)' section header above the inherited 2.5.x entries "
        "so the provenance is clearly identified."
    )


# ---------------------------------------------------------------------------
# docs/WORKFLOW_GIT_EPIC.md — Release Flow / Release Gate section
# ---------------------------------------------------------------------------


def _epic_workflow_text() -> str:
    path = _REPO_ROOT / "docs" / "WORKFLOW_GIT_EPIC.md"
    if not path.exists():
        import pytest as _pytest

        _pytest.skip("internal docs/WORKFLOW_GIT_EPIC.md not present in this checkout")
    return path.read_text(encoding="utf-8")


def test_workflow_git_epic_drops_no_release_train_prelude() -> None:
    text = _epic_workflow_text()
    assert "No Hivemind release train is active" not in text, (
        "docs/WORKFLOW_GIT_EPIC.md still carries the legacy 'No Hivemind "
        "release train is active' prelude. ADR-0018 / P6-8 requires this "
        "to be replaced by the active Release Flow / Release Gate "
        "section encoding the executable release gate."
    )


REQUIRED_SENTINELS: tuple[str, ...] = (
    "<!-- release-gate:semver-rationale -->",
    "<!-- release-gate:claims-check -->",
    "<!-- release-gate:mono-tenant-statement -->",
    "<!-- release-gate:migration-guide-link -->",
    "<!-- release-gate:image-build -->",
    "<!-- release-gate:smoke -->",
    "<!-- release-gate:human-go -->",
)


def test_workflow_git_epic_embeds_release_gate_sentinels() -> None:
    text = _epic_workflow_text()
    missing = [s for s in REQUIRED_SENTINELS if s not in text]
    assert not missing, (
        "docs/WORKFLOW_GIT_EPIC.md is missing the following ADR-0018 "
        "release-gate HTML-comment sentinels:\n"
        + "\n".join(f"  - {s}" for s in missing)
        + "\nEach sentinel must appear at the top of its corresponding "
        "sub-section (SemVer rationale, Claims/Non-claims check, Mandatory "
        "mono-tenant statement, Migration guide link, Image build, Smoke "
        "test, Human-confirmed publication gate)."
    )


def test_each_sentinel_precedes_a_subsection_heading() -> None:
    """Each sentinel must be followed within a few lines by an H3 heading.

    Defensive: a sentinel on its own with no section underneath would
    silently pass the "is present" check but provide no real structural
    anchor. We require an H3 within the next 5 non-blank lines.
    """
    text = _epic_workflow_text()
    lines = text.splitlines()
    for sentinel in REQUIRED_SENTINELS:
        idx = None
        for i, line in enumerate(lines):
            if sentinel in line:
                idx = i
                break
        assert idx is not None, f"sentinel {sentinel!r} not found (covered by prior test)"
        window = lines[idx + 1 : idx + 8]
        non_blank = [w for w in window if w.strip()]
        assert non_blank, f"sentinel {sentinel!r} has no following content"
        assert non_blank[0].lstrip().startswith("### "), (
            f"sentinel {sentinel!r} must be immediately followed by an "
            f"H3 sub-section heading; got {non_blank[0]!r}"
        )
