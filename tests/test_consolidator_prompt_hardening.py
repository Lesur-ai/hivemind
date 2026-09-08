"""Prompt contracts for source dates, concise synthesis, and bank size guidance.

Without a note's date, the model may mistake the consolidation day for the
date of a fact. These tests pin the note ``date``, measured file size, and
the date helper's no-guessing contract. The system-prompt rules that use
those inputs are pinned by text because they are the contract; rules 1-8
keep their numbers for ``tests/test_issue17_validation.py``.
"""

from __future__ import annotations

from unittest.mock import patch

import json
import re
from pathlib import Path

from live_mem.core.consolidator import (
    SYSTEM_PROMPT_ENGLISH,
    ConsolidatorService,
    _live_note_date,
)


def _flat(text: str) -> str:
    """Whitespace-normalized view: the prompt is hand-wrapped, the contract is not."""
    return " ".join(text.split())


def _service() -> ConsolidatorService:
    # Prompt construction needs no constructor state (same seam as the sibling
    # prompt tests): only the pure builder is exercised.
    return object.__new__(ConsolidatorService)


def _user_prompt(notes: list[dict], bank_files: list[dict]) -> str:
    messages = _service()._build_prompt("s", "rules", None, notes, bank_files)
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    return messages[1]["content"]


def _front_matter(**fields: str) -> str:
    body = "".join(f'{key}: "{value}"\n' for key, value in fields.items())
    return f"---\n{body}---\n"


class TestLiveNoteDate:
    def test_canonical_object_name_gives_the_day_written(self) -> None:
        assert _live_note_date("20260821T101500_agent-a_decision_abcd1234.md", None) == "2026-08-21"

    def test_agent_with_underscores_does_not_shift_the_timestamp(self) -> None:
        assert _live_note_date("20260807T090000_clr_agents_progress_deadbeef.md", None) == "2026-08-07"

    def test_impossible_day_falls_back_to_the_front_matter_timestamp(self) -> None:
        front_matter = 'agent: "a"\ntimestamp: "2026-08-22T10:15:00.123456+00:00"\ncategory: "decision"'
        assert _live_note_date("20260231T101500_a_decision_abcd1234.md", front_matter) == "2026-08-22"

    def test_no_prefix_and_no_front_matter_gives_no_date(self) -> None:
        assert _live_note_date("renamed-note.md", None) is None

    def test_malformed_front_matter_timestamp_is_not_guessed(self) -> None:
        assert _live_note_date("renamed-note.md", 'timestamp: "yesterday"') is None

    def test_front_matter_without_timestamp_gives_no_date(self) -> None:
        assert _live_note_date("renamed-note.md", 'agent: "a"\ncategory: "todo"') is None


class _NowOnlyClock:
    """The frozen-clock stub shape the integration harnesses install on
    ``live_mem.core.consolidator.datetime`` (``now()`` only, no parsers)."""

    @staticmethod
    def now(tz=None):  # noqa: ANN001 - mirrors the harness stubs
        raise AssertionError("the date parser must not consult the clock")


class TestFrozenClockIndependence:
    """Parsing through the replaceable clock seam broke prompt construction
    in every harness freezing ``consolidator.datetime``
    (``batch_prompt_failed``). The parser must ignore that seam entirely."""

    def test_object_name_date_survives_a_now_only_clock(self) -> None:
        with patch("live_mem.core.consolidator.datetime", _NowOnlyClock):
            assert _live_note_date("20260821T101500_agent-a_decision_abcd1234.md", None) == "2026-08-21"

    def test_front_matter_fallback_survives_a_now_only_clock(self) -> None:
        with patch("live_mem.core.consolidator.datetime", _NowOnlyClock):
            assert _live_note_date("renamed.md", 'timestamp: "2026-08-29T07:00:00+00:00"') == "2026-08-29"

    def test_prompt_construction_survives_a_now_only_clock(self) -> None:
        note = {
            "key": "s/live/20260821T101500_agent-a_decision_abcd1234.md",
            "content": _front_matter(agent="agent-a", category="decision") + "Body\n",
        }
        with patch("live_mem.core.consolidator.datetime", _NowOnlyClock):
            user = _user_prompt([note], [{"key": "s/bank/progress.md", "content": "# Progress\n"}])
        assert "[agent=agent-a, category=decision, date=2026-08-21] ---" in user
        assert "--- File: progress.md (11 bytes) ---" in user


class TestPromptInputs:
    def test_note_header_carries_the_day_written(self) -> None:
        note = {
            "key": "s/live/20260821T101500_agent-a_decision_abcd1234.md",
            "content": _front_matter(agent="agent-a", category="decision") + "We chose X.\n",
        }
        user = _user_prompt([note], [])
        assert "--- Note 1/1 [agent=agent-a, category=decision, date=2026-08-21] ---" in user

    def test_date_sits_between_category_and_tags(self) -> None:
        note = {
            "key": "s/live/20260821T101500_agent-a_decision_abcd1234.md",
            "content": _front_matter(agent="agent-a", category="decision", tags="x,y") + "Body\n",
        }
        user = _user_prompt([note], [])
        assert '[agent=agent-a, category=decision, date=2026-08-21, tags="x,y"]' in user

    def test_note_without_parsable_date_gets_no_date_field(self) -> None:
        note = {"key": "s/live/renamed.md", "content": "No front matter here.\n"}
        user = _user_prompt([note], [])
        assert "--- Note 1/1 [agent=unknown, category=unknown] ---" in user
        assert "date=" not in user.split("=== CURRENT BANK FILES ===")[0]

    def test_front_matter_timestamp_dates_a_renamed_note(self) -> None:
        note = {
            "key": "s/live/renamed.md",
            "content": _front_matter(agent="a", category="todo", timestamp="2026-08-29T07:00:00+00:00") + "Body\n",
        }
        user = _user_prompt([note], [])
        # identity and category still come from the front matter; only the date is new
        assert "[agent=a, category=todo, date=2026-08-29] ---" in user

    def test_bank_header_states_the_utf8_size_in_bytes(self) -> None:
        content = "# Active Context\n\nDécision é — ✓\n"
        byte_size = len(content.encode("utf-8"))
        assert byte_size != len(content), "the fixture must separate bytes from characters"
        user = _user_prompt([], [{"key": "s/bank/activeContext.md", "content": content}])
        assert f"--- File: activeContext.md ({byte_size} bytes) ---" in user
        assert f"({len(content)} bytes)" not in user
        assert "--- End file: activeContext.md ---" in user


class TestSystemPromptRules:
    def test_inputs_list_names_the_date_and_the_measured_size(self) -> None:
        assert "with their metadata: agent, category, date written, tags" in SYSTEM_PROMPT_ENGLISH
        assert "each with its measured size in bytes" in SYSTEM_PROMPT_ENGLISH

    def test_dating_rule_forbids_the_consolidation_day(self) -> None:
        assert "9. **Dates come from the notes, never from the calendar**" in SYSTEM_PROMPT_ENGLISH
        assert "the consolidation day is NOT a valid date" in SYSTEM_PROMPT_ENGLISH

    def test_synthesis_and_size_rules_replace_the_accumulation_bullet(self) -> None:
        # Synthesis must preserve facts and identifiers without pasting prose.
        synthesis = _flat(SYSTEM_PROMPT_ENGLISH)
        assert "ONE LINE PER FACT — SYNTHESIZE, NEVER PASTE" in synthesis
        assert "keep every identifier of its fact" in synthesis
        assert "Never reproduce a note's sentences word for word" in synthesis
        assert "A fact the batch brings that no line carries is a loss" in synthesis
        # Undated notes stay undated; required verbatim material is preserved.
        assert "a note without `date` yields an undated line — never an invented date, rule 9" in synthesis
        assert "a definition, a quotation, an exact project term" in synthesis
        # Overflow is allowed for identifiers AND for the verbatim
        # material the rules require; "never to keep prose" (unsatisfiable with a long
        # required quotation) is gone
        assert "or the verbatim material rule 2 and the RULES require" in synthesis
        assert "never for free prose" in synthesis and "never to keep prose" not in synthesis
        # ONE cardinality, the same sentence in the system prompt, the user
        # instruction 19 and the history bullet; the contradictory statements are gone
        assert synthesis.count(CARDINALITY) == 2   # the one-line bullet AND the history bullet, verbatim
        for gone in (
            "one per fact of the event",
            "one dated line per event",
            "an event with many facts takes several lines",
            "as many lines as there are distinct facts",
            "a fact dropped is a loss",
            "Summarize old entries (> 30 days)",
        ):
            assert gone not in synthesis, gone
        assert "the one-line pointer another file may receive is not a fact and is not counted" in synthesis
        assert "grow far less" not in synthesis and "one or two sentences per fact" not in synthesis
        assert "SIZE DISCIPLINE" in SYSTEM_PROMPT_ENGLISH
        flat = _flat(SYSTEM_PROMPT_ENGLISH)
        # An over-target file admits only necessary new facts; shrinking never
        # deletes content the batch has no source for. The rule must remain
        # satisfiable for an additive-only batch.
        assert "grows ONLY by the condensed minimum its new facts require" in flat
        assert "never omit or falsely discard a note to keep a file small" in flat
        assert "MUST NOT GROW" not in flat
        assert "MUST leave this batch SMALLER" not in flat
        assert "listed BEFORE the removal in file_edits (writes apply in order)" in flat
        # The superseded state is retired, not
        # archived; what no note touches is never deleted; age is never a reason
        assert "CLEAN ACTIVELY — RETIRE THE SUPERSEDED, KEEP THE DURABLE" in flat
        assert "MOVE — NEVER DROP" not in flat
        assert "then DELETE the superseded state, do not archive it" in flat
        assert "A decision the batch reverses keeps one line in the history file (dated the same way) naming what it replaced" in flat
        assert "Items the batch does not touch stay exactly as they are" in flat
        assert "age alone is never a reason — age-based condensation is compaction's job, a human decision" in flat
        assert "legacy size reduction belongs to compaction" in flat
        assert "delete_section is allowed ONLY for a section whose facts already live elsewhere in the bank" in flat
        assert "the superseded state itself is not preserved" in flat
        assert "A fact no note of this batch supersedes and that is present nowhere else in the bank is NEVER deleted" in flat
        assert "NEVER delete history: history records durable events" in flat
        assert "remove details from old sessions" not in flat

    def test_relocation_is_batch_motivated_and_preservation_precedes_removal(self) -> None:
        """The bank is a legitimate source for a move only when a
        note of the batch motivates it, and a replacement is recorded before removal."""
        flat = _flat(SYSTEM_PROMPT_ENGLISH)
        assert "Content that already exists in the CURRENT BANK FILES is a legitimate source for a MOVE or a CONDENSATION" in flat
        assert "only performed when a note of the batch motivates it" in flat
        assert "6. **Replace, but preserve first**" in flat
        assert "first RECORD the replacement in the history file" in flat
        assert "Never remove before the durable facts are preserved" in flat
        assert "Remove replaced items" not in flat
        assert "Do not silently preserve them" not in flat

    def test_response_example_preserves_in_history_before_deleting(self) -> None:
        """The example must CONTAIN the paired history append and
        list it before the current-context deletion — writes apply in file_edits order."""
        user = _user_prompt([], [])
        block = user.split("Return JSON with this exact structure:\n", 1)[1].split("\n=== IMPORTANT INSTRUCTIONS ===", 1)[0]
        example = json.loads(block)
        edits = example["file_edits"]
        first = edits[0]
        assert first["filename"] == "progress.md" and first["action"] == "edit"
        assert [op["type"] for op in first["operations"]] == ["append_to_section"]
        assert first["operations"][0]["notes"] == [1]
        deletes = [(i, op) for i, f in enumerate(edits) for op in f.get("operations", []) if op["type"] == "delete_section"]
        assert deletes, "the example must still show a delete_section"
        assert all(i > 0 for i, _ in deletes), "every deletion comes after the history-file preservation"
        assert all(op["notes"] == [1] for _, op in deletes), "the deletion cites the same motivating note"
        assert "A source decision explicitly replaces it." not in user

    def test_changelogs_do_not_promise_a_shrink(self) -> None:
        """The active release notes must state the final rule.

        In the private tree both sources exist. In the exported public tree the
        overlay IS the root CHANGELOG.md (export policy maps it there) and the
        private-only overlay path is absent, so only existing sources are read —
        the root changelog is always one of them.
        """
        root = Path(__file__).resolve().parents[1]
        sources = [root / "CHANGELOG.md", root / "release/public-overlay/CHANGELOG.md"]
        checked = [path for path in sources if path.exists()]
        assert checked and checked[0].name == "CHANGELOG.md", "the root changelog must exist in every tree"
        for path in checked:
            flat = _flat(path.read_text(encoding="utf-8"))
            assert "leave the batch smaller" not in flat, path.name
            assert "must leave the batch smaller" not in flat, path.name

    def test_rules_template_pairs_deletion_with_preservation(self) -> None:
        template = (Path(__file__).resolve().parents[1] / "RULES/live-mem.standard.memory.bank.md").read_text(encoding="utf-8")
        assert "delete sections superseded by newer versions once their durable facts are recorded in progress.md" in template
        # The template carries the same undated fallback as the system prompt
        assert (
            "one line per fact, dated with the note's date when it has one (an undated note yields an undated line, "
            "never an invented date), that keeps every identifier, never a copied paragraph"
        ) in template
        assert "one dated line per fact" not in template
        assert "the superseded state (an old status, value or transient state) is deleted, not archived; what no note touches stays" in template
        assert "ANTI-ACCUMULATION RULE" not in SYSTEM_PROMPT_ENGLISH

    def test_rules_one_to_eight_keep_their_numbers(self) -> None:
        assert "(rule #7)" in SYSTEM_PROMPT_ENGLISH
        assert "8. **`[inferred]` traceability markers**" in SYSTEM_PROMPT_ENGLISH

    def test_user_prompt_repeats_the_dating_rule_next_to_the_format(self) -> None:
        user = _user_prompt([], [])
        assert "17. Every date you write comes from the note's `date` field" in user
        assert (
            "18. Never delete content no note of this batch completes or supersedes; when a note supersedes an item, write "
            "its durable outcome first (one line, dated with the note's `date` when it has one — undated otherwise, never an "
            "invented date — in the file the rules assign, citing the note, listed BEFORE the removal in file_edits — writes "
            "apply in order) and delete the superseded state: an intermediate status, value "
            "or transient state leaves no line; an over-target file grows only by the condensed minimum its new facts "
            "require — age-based condensation is compaction's job, not yours"
        ) in user
        assert (
            "19. One line per fact in the file that owns it, dated with the note's `date` when it has one (never an invented "
            "date), about 200 characters as a target — longer only to keep every identifier or the verbatim material the "
            "rules require (a definition, a quotation, an exact term), never for free prose; no note sentence word for word "
            "except that required verbatim material; one cardinality everywhere: a line carries one fact, or the facts of "
            "one event when they fit together; a fact never spans two lines; no line without a fact (pointers in other "
            "files are not facts)"
        ) in user
        assert CARDINALITY in user


CARDINALITY = (
    "a line carries one fact, or the facts of one event when they fit together; "
    "a fact never spans two lines; no line without a fact"
)
_ROOT = Path(__file__).resolve().parents[1]
_DESIGN_CONTRACT = _ROOT / "DESIGN/live-mem/CONSOLIDATION_LLM.md"
_SURFACES = {
    "system": lambda: _flat(SYSTEM_PROMPT_ENGLISH),
    "user": lambda: _flat(_user_prompt([], [])),
    "rules-template": lambda: _flat((_ROOT / "RULES/live-mem.standard.memory.bank.md").read_text(encoding="utf-8")),
}
# The design contract ships only with the source tree that carries DESIGN/live-mem
# (the staged public tree has no DESIGN/live-mem directory at all): read it where
# it exists, never open a missing path.
if _DESIGN_CONTRACT.exists():
    _SURFACES["design-contract"] = lambda: _flat(_DESIGN_CONTRACT.read_text(encoding="utf-8"))
_CARDINALITY_SURFACES = tuple(n for n in ("system", "user", "design-contract") if n in _SURFACES)


class TestMaintainedSurfacesAgree:
    """The four maintained surfaces (system prompt, user instructions,
    rules template, design contract) must carry the SAME cardinality clause, the same
    verbatim-overflow exception and the same dating fallback — normalized equality and
    absence, not substrings of one formulation among several."""

    def test_one_cardinality_clause_verbatim_on_every_surface_that_states_cardinality(self) -> None:
        for name in _CARDINALITY_SURFACES:
            assert CARDINALITY in _SURFACES[name](), name
        for name, text in ((n, f()) for n, f in _SURFACES.items()):
            for gone in (
                "as many lines as distinct facts",
                "as many lines as there are distinct facts",
                "one dated line per event",
                "one per fact of the event",
                "an event with many facts takes several lines",
                "never a line without a fact",
                "no line is written without a fact",
            ):
                assert gone not in text, (name, gone)

    def test_overflow_exception_names_identifiers_and_required_verbatim_everywhere(self) -> None:
        for name in _CARDINALITY_SURFACES:
            text = _SURFACES[name]()
            assert "never for free prose" in text, name
            assert "longer only to keep every identifier;" not in text, name   # identifier-only overflow
            assert "never to keep prose" not in text, name

    def test_no_surface_demands_an_unconditionally_dated_line(self) -> None:
        """Round 3 F1: an undated note that supersedes state must not force an invented
        date — every outcome/decision/history line is dated only when the note is."""
        for name, text in ((n, f()) for n, f in _SURFACES.items()):
            assert re.search(r"\bone dated line\b", text) is None, name
            assert re.search(r"\bdated lines?,", text) is None, name
            assert "when it has one" in text, name

    def test_no_surface_still_moves_every_superseded_item_to_history(self) -> None:
        """Round 3 MEDIUM: the retire-the-superseded doctrine has no move-everything remnant."""
        for name, text in ((n, f()) for n, f in _SURFACES.items()):
            for gone in (
                "move to the history file what the batch completes or supersedes",
                "moves to the history file first",
                "MOVE — NEVER DROP",
                "lands in the history/progress file",
            ):
                assert gone not in text, (name, gone)
        assert "retire the superseded state" in _SURFACES["system"]()
        if "design-contract" in _SURFACES:
            assert "Retire the superseded, keep the durable" in _SURFACES["design-contract"]()

    def test_source_tree_always_checks_the_design_contract(self) -> None:
        """The optional surface is optional only where DESIGN/live-mem is absent
        (the staged public tree): wherever that directory exists, the design
        contract must exist and be checked."""
        if _DESIGN_CONTRACT.parent.is_dir():
            assert "design-contract" in _SURFACES, _DESIGN_CONTRACT
