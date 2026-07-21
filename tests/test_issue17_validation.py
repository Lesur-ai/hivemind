# -*- coding: utf-8 -*-
"""
Tests — Issue #17 — Validation pass `unattributed_claims_count` + `[inféré]` markers.

Strategy: purely code-only tests (deterministic, zero LLM calls).
Each test describes a bank-write scenario and verifies that the
unsourced-claim detector produces the expected verdict.

Convention: ``test_FIXNAME_blocks_ATTACK`` when the test proves that a
fake claim is correctly detected (proof by contraposition).

Note: French test strings (e.g. ``"Bug résolu hier"``, ``"15/05/2026"``)
are intentional — they exercise the French-aware detection used by the
default `general` ontology.
"""

from __future__ import annotations

import pytest

from live_mem.core.consolidator import (
    SYSTEM_PROMPT,
    _validate_unattributed_claims,
    _extract_claim_tokens,
    _has_strong_status_claim,
    _normalize_for_match,
    _INFERRED_MARKER_RE,
)


# =============================================================================
# Internal helpers — `_extract_claim_tokens`, `_has_strong_status_claim`, etc.
# =============================================================================


class TestExtractClaimTokens:
    """`_extract_claim_tokens` must extract verifiable signatures."""

    def test_extracts_metric_with_tests(self):
        tokens = _extract_claim_tokens("171/171 tests PASS")
        # "171" should be matched via the "171 tests" unit pattern.
        assert any("171" in t for t in tokens), f"got {tokens}"

    def test_extracts_percentage(self):
        tokens = _extract_claim_tokens("Réduction de 80% sur les batches")
        assert any("80%" in t.replace(" ", "") for t in tokens), f"got {tokens}"

    def test_extracts_iso_date(self):
        tokens = _extract_claim_tokens("Mergé le 2026-05-15")
        assert "2026-05-15" in tokens

    def test_extracts_french_date(self):
        tokens = _extract_claim_tokens("Mergé le 15/05/2026")
        assert "15/05/2026" in tokens

    def test_extracts_short_date(self):
        tokens = _extract_claim_tokens("Phase démarrée le 12/03")
        assert "12/03" in tokens

    def test_extracts_version(self):
        tokens = _extract_claim_tokens("Release v2.0.0 publiée")
        assert "v2.0.0" in tokens

    def test_extracts_pr_ref(self):
        tokens = _extract_claim_tokens("PR #14 fermée")
        assert "#14" in tokens

    def test_returns_empty_on_pure_structural_line(self):
        # No digit, no date, no version, no #ref.
        assert _extract_claim_tokens("## Section title") == set()
        assert _extract_claim_tokens("- Bullet sans chiffre") == set()
        assert _extract_claim_tokens("") == set()


class TestHasStrongStatusClaim:
    """`_has_strong_status_claim` detects claimed state changes."""

    @pytest.mark.parametrize(
        "line",
        [
            "Bug résolu hier",
            "Bug resolu hier",
            "PR mergé",
            "Branch merged",
            "v2.0.0 publié",
            "Issue fermée",
            "Tests passed",
            "Tests failed",
            "Build OK",
        ],
    )
    def test_detects_status_keywords(self, line):
        assert _has_strong_status_claim(line), f"missed status in: {line}"

    @pytest.mark.parametrize(
        "line",
        [
            "Réflexion sur l'architecture",
            "## Focus actuel",
            "- Tâche en cours",
        ],
    )
    def test_no_status_on_neutral_lines(self, line):
        assert not _has_strong_status_claim(line)


class TestNormalizeForMatch:
    """`_normalize_for_match` must preserve key tokens untouched."""

    def test_keeps_version_intact(self):
        assert "v2.0.0" in _normalize_for_match("Version v2.0.0 publiée")

    def test_keeps_pr_ref_intact(self):
        assert "#14" in _normalize_for_match("PR #14 mergée")

    def test_keeps_percentage(self):
        # We keep the digit and its attached %.
        normalized = _normalize_for_match("Réduction 80% obtenue")
        assert "80%" in normalized

    def test_strips_punctuation_around_numbers(self):
        # "171/171" must survive surrounding punctuation.
        normalized = _normalize_for_match("Total: 171/171 tests, OK.")
        assert "171/171" in normalized

    def test_case_insensitive(self):
        assert _normalize_for_match("ABC") == _normalize_for_match("abc")


class TestInferredMarkerRegex:
    """The `[inféré]` regex must recognize LLM variants."""

    @pytest.mark.parametrize(
        "line",
        [
            "Phase 2 terminée [inféré]",
            "Phase 2 terminée [inféré, suite progress Phase 3]",
            "Phase 2 terminée [INFÉRÉ]",
            "Phase 2 terminée [Inféré, raison]",
        ],
    )
    def test_matches_variants(self, line):
        assert _INFERRED_MARKER_RE.search(line) is not None, f"missed in: {line}"

    @pytest.mark.parametrize(
        "line",
        [
            "Phase 2 terminée",  # no marker
            "Phase 2 inféré sans crochets",  # no brackets
            "[infered] mauvais accent",  # missing accent
        ],
    )
    def test_rejects_non_marker(self, line):
        assert _INFERRED_MARKER_RE.search(line) is None


# =============================================================================
# `_validate_unattributed_claims` — proofs by contraposition
# =============================================================================


def _note(content: str) -> dict:
    """Helper: build a minimal note (just its `content`)."""
    return {"content": content}


class TestValidateUnattributedClaims_HappyPaths:
    """Cases where the consolidation is correctly sourced → 0 unsourced claim."""

    def test_no_changes_returns_zero(self):
        """If the bank did not change, no claim was added."""
        before = {"activeContext.md": "## Focus\nRien"}
        after = before.copy()
        result = _validate_unattributed_claims(before, after, [_note("note 1")], 20)
        assert result["unattributed_claims_count"] == 0
        assert result["lines_added"] == 0

    def test_sourced_metric_is_attributed(self):
        """Metric 171/171 is present in a note → the claim is attributed."""
        before = {"progress.md": "# Progress\n"}
        after = {"progress.md": "# Progress\n- 171/171 tests PASS"}
        notes = [_note("Suite complète : 171/171 tests PASS, aucune régression.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 0
        assert result["lines_scanned"] >= 1, "the metric line must be scanned"

    def test_sourced_date_is_attributed(self):
        before = {"progress.md": ""}
        after = {"progress.md": "- v2.0.0 publiée le 15/05/2026"}
        notes = [_note("Release v2.0.0 mergée sur main le 15/05/2026.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 0

    def test_sourced_pr_ref_is_attributed(self):
        before = {"progress.md": ""}
        after = {"progress.md": "- PR #14 mergée"}
        notes = [_note("PR #14 review terminée, prête à merge.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 0

    def test_inferred_marker_excludes_line(self):
        """A line tagged `[inféré]` must NOT be counted as unsourced,
        even if its tokens are missing from the notes."""
        before = {"progress.md": ""}
        # "999 jours" is NOT in the note → but the marker is explicit.
        after = {"progress.md": "- 999 jours écoulés [inféré, suite migration]"}
        notes = [_note("Migration en cours.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 0
        assert result["inferred_claims_count"] == 1


class TestValidateUnattributedClaims_DetectsHallucinations:
    """Proofs by contraposition — without the pass, hallucinations slip through."""

    def test_blocks_invented_metric(self):
        """The LLM invents 999/999 tests; the note doesn't mention it."""
        before = {"progress.md": "# Progress\n"}
        after = {"progress.md": "# Progress\n- 999/999 tests PASS"}
        notes = [_note("Travail en cours sur l'authentification.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 1
        assert any(
            "999/999" in ex["line"] or "999" in " ".join(ex["tokens"])
            for ex in result["examples"]
        ), f"examples: {result['examples']}"

    def test_blocks_invented_date(self):
        before = {"progress.md": ""}
        after = {"progress.md": "- Migration lancée le 2024-01-01"}
        notes = [_note("Migration en cours, pas de date précise.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 1

    def test_blocks_invented_pr_ref(self):
        before = {"progress.md": ""}
        after = {"progress.md": "- PR #9999 reviewée"}
        notes = [_note("Quelques notes sans référence à des PRs précises.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 1

    def test_blocks_invented_version(self):
        before = {"progress.md": ""}
        after = {"progress.md": "- Release v99.99.99 publiée"}
        notes = [_note("Préparation d'une release prochaine.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 1

    def test_blocks_invented_status_without_source(self):
        """Strong status 'résolu' with NO source in the notes."""
        before = {"progress.md": "## Bugs\n- bug X ouvert"}
        after = {"progress.md": "## Bugs\n- bug X ouvert\n- bug Y résolu"}
        # The note mentions neither bug Y nor any resolution.
        notes = [_note("Sprint planning en cours.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 1

    def test_accepts_status_when_mentioned_in_notes(self):
        """Mirror of the previous case: the note says 'résolu' → the line is OK."""
        before = {"progress.md": ""}
        after = {"progress.md": "- bug Y résolu"}
        notes = [_note("Le bug Y a été résolu lors de la session de hier.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 0


class TestValidateUnattributedClaims_BorneExamples:
    """The examples counter must be bounded by `max_examples`."""

    def test_examples_capped(self):
        before = {"f.md": ""}
        # 10 unsourced claims in a single file.
        after_lines = [f"- {i}/{i} tests PASS" for i in range(100, 110)]
        after = {"f.md": "\n".join(after_lines)}
        notes = [_note("Rien à voir.")]
        result = _validate_unattributed_claims(before, after, notes, max_examples=3)
        assert result["unattributed_claims_count"] == 10
        assert len(result["examples"]) == 3, "examples must be capped to 3"

    def test_zero_examples_when_max_is_zero(self):
        before = {"f.md": ""}
        after = {"f.md": "- 99/99 tests"}
        notes = [_note("rien")]
        result = _validate_unattributed_claims(before, after, notes, max_examples=0)
        assert result["unattributed_claims_count"] == 1
        assert result["examples"] == []


class TestValidateUnattributedClaims_DiffOnly:
    """The pass must inspect ONLY added lines (diff)."""

    def test_existing_unsourced_lines_are_not_flagged(self):
        """Pre-existing lines (possibly unsourced from earlier
        consolidations) MUST NEVER be scanned: we only look at what
        the current batch adds."""
        old_line = "- 42 tests PASS (vieille entrée jamais sourcée)"
        before = {"progress.md": old_line}
        # No change → 0 even though the line carries an unsourced claim.
        after = {"progress.md": old_line}
        notes = [_note("Travail en cours.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 0

    def test_new_file_with_unsourced_metric_is_flagged(self):
        before = {}
        after = {"new.md": "- 555 tests PASS"}
        notes = [_note("Démarrage projet.")]
        result = _validate_unattributed_claims(before, after, notes, 20)
        assert result["unattributed_claims_count"] == 1


# =============================================================================
# SYSTEM_PROMPT — rule #8 [inféré]
# =============================================================================


class TestSystemPromptRule8:
    """SYSTEM_PROMPT must contain the `[inféré]` rule."""

    def test_rule_8_present_in_system_prompt(self):
        assert "[inféré]" in SYSTEM_PROMPT, (
            "Without rule #8 the LLM will never flag its inferences, "
            "and the validation pass will report false positives."
        )

    def test_rule_8_mentions_inference_transitive(self):
        # Rule #8 must reference transitive inference or logical deduction,
        # so the LLM knows WHEN to apply the marker.
        assert (
            "INFÉRENCE TRANSITIVE" in SYSTEM_PROMPT
            or "déduction logique" in SYSTEM_PROMPT
        )

    def test_rule_8_provides_examples(self):
        # Check that at least one literal example from the rule is present.
        # This protects against prompt regressions that would strip the
        # examples (crucial for smaller models).
        assert "Migration terminée [inféré]" in SYSTEM_PROMPT


# =============================================================================
# Config — opt-in default OFF
# =============================================================================


class TestValidationConfig:
    """Issue #17 ENV vars MUST be opt-in (default OFF)."""

    def test_validation_disabled_by_default(self):
        # Import directly from Settings (not the singleton) to test the
        # default value in isolation.
        from live_mem.config import Settings

        s = Settings()
        assert s.consolidation_validation_enabled is False, (
            "Issue #17 must be opt-in (zero impact on existing deployments "
            "until the feature is explicitly enabled)."
        )

    def test_validation_max_examples_default_is_bounded(self):
        from live_mem.config import Settings

        s = Settings()
        # Reasonable bounds: not too high to avoid huge payloads,
        # not too low to remain informative.
        assert 1 <= s.consolidation_validation_max_examples <= 100

    def test_validation_can_be_enabled_via_env(self, monkeypatch):
        from live_mem.config import Settings

        monkeypatch.setenv("CONSOLIDATION_VALIDATION_ENABLED", "true")
        s = Settings()
        assert s.consolidation_validation_enabled is True
