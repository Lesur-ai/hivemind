# -*- coding: utf-8 -*-
"""P6-8 release-gate non-claims lint (ADR-0018).

Stdlib-only, offline, deterministic. Sweeps release-facing Markdown surfaces
for the seven forbidden V1 non-claims tokens enumerated in
``docs/adr/0018-public-release-naming-versioning.md`` line 154 (binding
non-claims guardrail) and rejects any occurrence that is **not** inside a
recognised non-claims fence.

Three fence styles are accepted (DECISION C in the P6-8 plan review):

  (a) HTML-comment fence pair, case-insensitive::

        <!-- non-claims -->
        ... forbidden tokens allowed here ...
        <!-- /non-claims -->

  (b) An H2/H3 heading whose normalised text (case-insensitive, emoji /
      symbol stripped via ``unicodedata.category``) matches one of
      ``non-claims`` / ``does not claim`` / ``not claim`` /
      ``hivemind does not claim`` — terminated by the next heading at
      equal or lesser depth.

  (c) A markdown blockquote LINE (``> ...``) that contains BOTH a
      forbidden token AND a markdown anchor link ``[...](#...)``. Such a
      line is treated as a documented disclaimer that references the
      non-claims section.

Positive assertion: ``CHANGELOG.md`` must contain the literal substring
``mono-tenant`` within the first 200 lines OR within its ``[Unreleased]``
section header, and the [Unreleased] header must be the Hivemind public
release header (clearly identified).

This test does NOT depend on ``live_mem``, ``boto3``, network, S3 or LLM. It
runs purely from the repo working copy resolved via ``__file__``.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Release-facing surfaces (DECISION C). Per DECISION J the integration guides
# are LEFT IN this sweep BUT the test is parametrised so a missing file is
# skipped silently — that lets P6-8 land before P6-4 rewrites the integration
# guides without taking a hard dependency on rebase order. Once P6-4 lands,
# the sweep is already wired and protects regressions automatically.
RELEASE_FACING_SURFACES: tuple[str, ...] = (
    "README.md",
    "README.fr.md",
    "FAQ.md",
    "FAQ.fr.md",
    "CHANGELOG.md",
    "CODEX_INTEGRATION.md",
    "CODEX_INTEGRATION.fr.md",
    "CLAUDE_CODE_INTEGRATION.md",
    "CLAUDE_CODE_INTEGRATION.fr.md",
    "docs/AGENT_MEMORY_SETUP.md",
    "docs/SECURITY.md",
    "docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.md",
    "docs/MIGRATION_LIVE_GRAPH_TO_HIVEMIND.fr.md",
    "docs/EXTENSION_POINTS.md",
    "docs/DEPLOYMENT.md",
)

# ADR-0018 §Non-claims guardrail (line 154) — the 8 forbidden tokens
# (counting the 7-item plan + "multi-tenant" as the 8th). Matched
# case-insensitively as bare substrings; the fence-detection logic decides
# whether each hit is allowed.
FORBIDDEN_TOKENS: tuple[str, ...] = (
    "quorum",
    "hub topology",
    "permanent master",
    "leader runtime",
    "CRDT",
    "multi-space merge",
    "parallel consolidation",
    "multi-tenant",
)

_HEADING_FENCE_PATTERNS = (
    re.compile(r"non[- ]?claims", re.IGNORECASE),
    re.compile(r"non[- ]?(?:pr[ée]tention|revendication)s?", re.IGNORECASE),
    re.compile(r"does\s+not\s+claim", re.IGNORECASE),
    re.compile(r"not\s+claim", re.IGNORECASE),
    re.compile(r"hivemind\s+does\s+not\s+claim", re.IGNORECASE),
    re.compile(r"ne\s+(?:revendique|pr[ée]tend)\s+pas", re.IGNORECASE),
    re.compile(r"ne\s+pr[ée]tend\s+pas", re.IGNORECASE),
)

_HTML_OPEN_FENCE = re.compile(r"<!--\s*non-claims\s*-->", re.IGNORECASE)
_HTML_CLOSE_FENCE = re.compile(r"<!--\s*/non-claims\s*-->", re.IGNORECASE)

_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")
_ANCHOR_LINK_RE = re.compile(r"\[[^\]]+\]\(#(?P<anchor>[^)\s]+)\)")

# Codex P6-8 review #4: blockquote-disclaimer style (c) used to accept ANY
# in-page anchor, so a blockquote with a forbidden token AND
# `[details](#quickstart)` would silently pass. Require the anchor itself to
# look like a non-claims anchor (English EN, English alt, and French).
_NON_CLAIMS_ANCHOR_RE = re.compile(
    r"(?:non[-_ ]?claims?|does[-_ ]?not[-_ ]?claim|"
    r"not[-_ ]?claim|hivemind[-_ ]?ne[-_ ]?revendique[-_ ]?pas|"
    r"ne[-_ ]?(?:revendique|pr[ée]tend)[-_ ]?pas|"
    r"non[-_ ]?(?:pr[ée]tention|revendication)s?)",
    re.IGNORECASE,
)


def _normalise_heading_text(text: str) -> str:
    """Strip emoji / symbol code points so heading matching is robust.

    Markdown headings frequently start with an emoji (``## 🚫 ...``) — we
    drop every Symbol (``S*``) and Other (``Cn``) code point and collapse
    whitespace before applying the fence patterns.
    """
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        # So = Other Symbol (emoji); Sk = Modifier Symbol; Cn = unassigned
        if cat.startswith("S") or cat == "Cn":
            continue
        kept.append(ch)
    cleaned = "".join(kept).strip()
    return re.sub(r"\s+", " ", cleaned)


def _is_heading_a_non_claims_fence(text: str) -> bool:
    normalised = _normalise_heading_text(text)
    return any(p.search(normalised) for p in _HEADING_FENCE_PATTERNS)


def _compute_fence_intervals(lines: list[str]) -> list[tuple[int, int]]:
    """Return [start_line, end_line] (1-based, inclusive) fenced intervals.

    Combines HTML-comment fence pairs and non-claims heading sections.
    """
    intervals: list[tuple[int, int]] = []

    # (a) HTML-comment fences.
    open_idx: int | None = None
    for idx, line in enumerate(lines, start=1):
        if _HTML_OPEN_FENCE.search(line) and open_idx is None:
            open_idx = idx
            continue
        if _HTML_CLOSE_FENCE.search(line) and open_idx is not None:
            intervals.append((open_idx, idx))
            open_idx = None
    # An unterminated <!-- non-claims --> open fence does NOT silence the rest
    # of the file: we only honour balanced pairs. The lint then reports any
    # tokens after the open fence as out-of-fence, which is the correct
    # fail-closed behaviour.

    # (b) H2/H3 fence headings — the section extends until the next heading
    # at equal-or-lesser depth.
    headings: list[tuple[int, int, str]] = []  # (line_no, depth, text)
    for idx, line in enumerate(lines, start=1):
        m = _HEADING_RE.match(line)
        if m:
            depth = len(m.group("hashes"))
            headings.append((idx, depth, m.group("text")))

    for i, (line_no, depth, text) in enumerate(headings):
        if depth not in (2, 3):
            continue
        if not _is_heading_a_non_claims_fence(text):
            continue
        # Find the next heading at equal or lesser depth.
        end_line = len(lines)
        for j in range(i + 1, len(headings)):
            other_line, other_depth, _ = headings[j]
            if other_depth <= depth:
                end_line = other_line - 1
                break
        intervals.append((line_no, end_line))

    return intervals


def _line_is_in_intervals(line_no: int, intervals: list[tuple[int, int]]) -> bool:
    return any(start <= line_no <= end for start, end in intervals)


def _line_is_blockquote_disclaimer(line: str, forbidden_token: str) -> bool:
    """Fence style (c): blockquote that links to a non-claims anchor.

    Codex P6-8 review #4 tightened this: the anchor MUST itself look like a
    non-claims anchor (matches ``_NON_CLAIMS_ANCHOR_RE``). The previous
    impl accepted ANY in-page anchor, so a blockquote with a forbidden
    token AND ``[details](#quickstart)`` would silently pass.
    """
    stripped = line.lstrip()
    if not stripped.startswith(">"):
        return False
    if forbidden_token.lower() not in line.lower():
        return False
    # Every anchor on the line is a candidate; at least one must look like a
    # non-claims anchor.
    for m in _ANCHOR_LINK_RE.finditer(line):
        anchor = m.group("anchor")
        if _NON_CLAIMS_ANCHOR_RE.search(anchor):
            return True
    return False


def _scan_file(rel_path: str) -> tuple[bool, list[str]]:
    """Return ``(file_exists, violations)`` for ``rel_path``."""
    path = _REPO_ROOT / rel_path
    if not path.exists():
        return False, []
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    intervals = _compute_fence_intervals(lines)

    violations: list[str] = []
    for line_no, line in enumerate(lines, start=1):
        lowered = line.lower()
        for token in FORBIDDEN_TOKENS:
            if token.lower() not in lowered:
                continue
            # Allow if line falls inside any non-claims fence interval.
            if _line_is_in_intervals(line_no, intervals):
                continue
            # Allow style (c): blockquote disclaimer that links to a non-
            # claims anchor and itself contains the forbidden token.
            if _line_is_blockquote_disclaimer(line, token):
                continue
            violations.append(
                f"{rel_path}:{line_no}: forbidden V1 non-claims token "
                f"{token!r} outside any non-claims fence -> {line.rstrip()!r}"
            )
    return True, violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel_path", RELEASE_FACING_SURFACES)
def test_release_facing_surface_has_no_unfenced_forbidden_tokens(
    rel_path: str,
) -> None:
    exists, violations = _scan_file(rel_path)
    if not exists:
        pytest.skip(f"{rel_path} not present in this worktree (skipping)")
    assert not violations, (
        f"\nADR-0018 §Non-claims guardrail violation(s) in {rel_path}:\n"
        + "\n".join(f"  - {v}" for v in violations)
        + "\n\nFix by wrapping each occurrence in one of the three accepted "
        "fence styles (HTML-comment pair, non-claims H2/H3 heading section, "
        "or a single blockquote line with both the token AND a markdown "
        "anchor link to the non-claims section)."
    )


def test_changelog_contains_mono_tenant_statement_in_head() -> None:
    """ADR-0018 §Mandatory mono-tenant statement.

    The statement must live in the changelog HEAD (first 200 lines): under
    ``[Unreleased]`` before a release cut, in the newest versioned section
    after it (see docs/WORKFLOW_GIT_EPIC.md §Mandatory mono-tenant statement).
    """
    path = _REPO_ROOT / "CHANGELOG.md"
    assert path.exists(), "CHANGELOG.md must exist at repo root"
    text = path.read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:200])
    assert "mono-tenant" in head, (
        "CHANGELOG.md MUST contain the literal substring 'mono-tenant' "
        "within the first 200 lines (ADR-0018 §Mandatory mono-tenant "
        "statement). Add the mandatory statement to the changelog head — "
        "under [Unreleased] before a release cut, in the newest versioned "
        "section after it."
    )


def test_changelog_unreleased_header_is_hivemind_public_release() -> None:
    """ADR-0018 §Release Readiness — [Unreleased] must be clearly identified.

    The lint accepts ``## [Unreleased] - Hivemind public release`` or any
    case-insensitive variant containing both ``[Unreleased]`` and
    ``hivemind``.
    """
    path = _REPO_ROOT / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    found = False
    for line in text.splitlines():
        if not line.startswith("## "):
            continue
        lowered = line.lower()
        if "[unreleased]" in lowered and "hivemind" in lowered:
            found = True
            break
    assert found, (
        "CHANGELOG.md MUST carry a header line of the form "
        "'## [Unreleased] - Hivemind public release' (or equivalent containing "
        "both '[Unreleased]' and 'hivemind') so consumers can distinguish "
        "the active Hivemind line from the inherited Live Memory provenance "
        "section."
    )


def test_non_claims_fence_detection_self_test() -> None:
    """Self-test on a synthetic fixture: each fence style must silence hits."""
    fixture = """
# Doc

quorum  -- this MUST be flagged

<!-- non-claims -->
quorum  -- inside HTML fence, allowed
<!-- /non-claims -->

## What Hivemind does NOT claim
quorum  -- inside heading fence, allowed
hub topology  -- ditto

## Next section
quorum  -- back outside, MUST be flagged

> See [non-claims](#non-claims) — Project Mesh V1 / Mesh Sync V1 is not quorum.
"""
    lines = fixture.splitlines()
    intervals = _compute_fence_intervals(lines)

    # Two intervals expected: HTML fence + heading fence.
    assert len(intervals) == 2, f"expected 2 intervals, got {intervals}"

    # Quorum lines that must be FLAGGED (outside fences).
    flag_line_numbers: list[int] = []
    for line_no, line in enumerate(lines, start=1):
        if "quorum" not in line.lower():
            continue
        if _line_is_in_intervals(line_no, intervals):
            continue
        if _line_is_blockquote_disclaimer(line, "quorum"):
            continue
        flag_line_numbers.append(line_no)

    # Expect exactly the two "MUST be flagged" lines.
    flagged_text = [lines[n - 1].strip() for n in flag_line_numbers]
    must_flag = [t for t in flagged_text if "MUST be flagged" in t]
    assert len(must_flag) == 2, (
        f"self-test expected 2 'MUST be flagged' lines outside fences, got "
        f"{len(must_flag)}: {flagged_text}"
    )


def test_blockquote_disclaimer_requires_non_claims_anchor() -> None:
    """Codex P6-8 review #4: anchor must look like a non-claims anchor.

    Two negative cases prove an unrelated anchor (``#installation``,
    ``#quickstart``) does NOT silence a forbidden token. Two positive
    cases prove the recognised English and French non-claims anchors
    DO silence them.
    """
    # NEGATIVE — unrelated anchor must NOT silence the token.
    neg_install = (
        "> Hivemind is not a quorum system ([details](#installation))"
    )
    assert not _line_is_blockquote_disclaimer(neg_install, "quorum"), (
        "blockquote with #installation anchor must NOT count as a "
        "non-claims disclaimer (Codex P6-8 review #4)"
    )

    neg_quickstart = (
        "> CRDT is not used here, see [docs](#quickstart) for details."
    )
    assert not _line_is_blockquote_disclaimer(neg_quickstart, "crdt"), (
        "blockquote with #quickstart anchor must NOT count as a "
        "non-claims disclaimer (Codex P6-8 review #4)"
    )

    # POSITIVE — non-claims anchor MUST silence the token.
    pos_en = (
        "> Hivemind is not a quorum system ([details](#non-claims))"
    )
    assert _line_is_blockquote_disclaimer(pos_en, "quorum"), (
        "blockquote with #non-claims anchor MUST count as a non-claims "
        "disclaimer (English)"
    )

    pos_fr = (
        "> Hivemind ne revendique pas le quorum "
        "([détails](#hivemind-ne-revendique-pas))"
    )
    assert _line_is_blockquote_disclaimer(pos_fr, "quorum"), (
        "blockquote with #hivemind-ne-revendique-pas anchor MUST count "
        "as a non-claims disclaimer (French)"
    )

    pos_does_not_claim = (
        "> Hivemind does not implement CRDT ([details](#does-not-claim))"
    )
    assert _line_is_blockquote_disclaimer(pos_does_not_claim, "crdt"), (
        "blockquote with #does-not-claim anchor MUST count as a "
        "non-claims disclaimer"
    )
