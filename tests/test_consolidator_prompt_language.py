"""Language contract for every server-owned consolidator prompt: English only.

The French compatibility bridge (``CONSOLIDATION_LEGACY_FRENCH_PROMPTS``)
is no longer supported. Every prompt the service owns
— main consolidation, corrective completion, duplicate-section merge, compaction —
is English, so a conversation can never switch language between the first
completion and the corrective one.
Operator-owned inputs (rules, notes, bank text) are still relayed verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from types import SimpleNamespace

import pytest

from hivemind_inference.records import ChatResult
from live_mem.core import consolidator as consolidator_module
from live_mem.core.consolidator import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_ENGLISH,
    ConsolidatorService,
    _corrective_messages,
)


@dataclass
class RecordingCompletion:
    """Offline completion seam that records the exact prompt sent to the model."""

    text: str = "merged"
    calls: list[dict] = field(default_factory=list)

    async def __call__(self, messages, output_budget, *, retry_policy="bounded"):
        self.calls.append(
            {
                "messages": messages,
                "output_budget": output_budget,
                "retry_policy": retry_policy,
            }
        )
        return ChatResult(
            text=self.text,
            configured_model="test-model",
            model_evidence="configured_only",
            finish_reason="stop",
        )


def _service() -> ConsolidatorService:
    service = object.__new__(ConsolidatorService)
    service._max_tokens = 4096
    service._context_window = 131_072
    service._timeout = 1
    service._complete_chat = RecordingCompletion()
    return service


def _main_messages() -> list[dict]:
    return _service()._build_prompt(
        space_id="language-contract",
        rules="# Règles exactes\n\n- Keep `memory_id` unchanged.",
        synthesis="Synthèse historique exacte.",
        notes=[
            {
                "key": (
                    "language-contract/live/"
                    "20260801T000000_agent-a_decision_11111111.md"
                ),
                "content": "Décision source exacte avec `memory_id`.",
            }
        ],
        bank_files=[
            {
                "key": "language-contract/bank/activeContext.md",
                "content": "# Contexte Actif\n\n## Focus Actuel\n\nTexte existant.",
            }
        ],
    )


def test_french_bridge_is_gone_from_the_service_and_its_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bridge is removed, not merely defaulted: no French prompt constant,
    no snapshotted flag, and a stale settings attribute changes nothing."""
    from live_mem.core import inference_runtime

    assert not hasattr(consolidator_module, "SYSTEM_PROMPT_FRENCH")
    settings = SimpleNamespace(
        consolidation_timeout=600,
        consolidation_transient_retries=3,
        consolidation_max_notes=200,
        consolidation_batch_size=3,
        consolidation_legacy_french_prompts=True,  # stale operator value: ignored
        consolidation_cooldown_seconds=60,
        bank_file_max_size=15360,
        consolidation_validation_enabled=False,
        consolidation_validation_max_examples=20,
    )
    runtime = SimpleNamespace(config=SimpleNamespace(chat=None))
    monkeypatch.setattr(consolidator_module, "get_settings", lambda: settings)
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: runtime)

    service = ConsolidatorService()

    assert not hasattr(service, "_legacy_french_prompts")


@pytest.mark.parametrize(
    ("profile_source", "expected_setting"),
    [
        ("inference", "INFERENCE_CHAT_CONTEXT_WINDOW"),
        ("llmaas-legacy", "LLMAAS_CONTEXT_WINDOW"),
    ],
)
def test_context_window_diagnostic_tracks_resolved_profile_family(
    profile_source: str,
    expected_setting: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import inference_runtime

    settings = SimpleNamespace(
        consolidation_timeout=600,
        consolidation_transient_retries=3,
        consolidation_max_notes=200,
        consolidation_batch_size=3,
        consolidation_cooldown_seconds=60,
        bank_file_max_size=15360,
        consolidation_validation_enabled=False,
        consolidation_validation_max_examples=20,
    )
    chat = SimpleNamespace(
        configured_model="test-model",
        context_window=4096,
        max_output_tokens=1024,
        source=profile_source,
    )
    runtime = SimpleNamespace(config=SimpleNamespace(chat=chat))
    monkeypatch.setattr(consolidator_module, "get_settings", lambda: settings)
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: runtime)

    service = ConsolidatorService()

    assert service._context_window_env_name == expected_setting


def test_english_system_prompt_is_the_public_default_alias() -> None:
    assert SYSTEM_PROMPT is SYSTEM_PROMPT_ENGLISH
    assert SYSTEM_PROMPT_ENGLISH.startswith(
        "You are an assistant specialized in maintaining project Memory Banks."
    )
    assert "Write all generated bank prose and the residual synthesis in English" in (
        SYSTEM_PROMPT_ENGLISH
    )
    assert "[inferred]" in SYSTEM_PROMPT_ENGLISH


def test_main_prompt_is_english_and_preserves_source_material_verbatim() -> None:
    messages = _main_messages()
    system_prompt, user_prompt = [message["content"] for message in messages]

    assert system_prompt == SYSTEM_PROMPT_ENGLISH
    assert "Tu es un assistant spécialisé" not in system_prompt
    assert '=== RULES FOR SPACE "language-contract" ===' in user_prompt
    assert "=== PREVIOUS SYNTHESIS ===" in user_prompt
    assert "=== LIVE NOTES TO INTEGRATE (1 notes) ===" in user_prompt
    # The day written and the measured size travel with the inputs.
    assert "[agent=agent-a, category=decision, date=2026-08-01]" in user_prompt
    assert "--- File: activeContext.md (" in user_prompt
    assert " bytes) ---\n" in user_prompt
    assert "--- End file: activeContext.md ---" in user_prompt
    assert "SYNTHÈSE PRÉCÉDENTE" not in user_prompt
    assert "catégorie=decision" not in user_prompt
    assert "--- Fichier:" not in user_prompt
    assert '"content": "New section content..."' in user_prompt
    assert "The residual synthesis must summarize the processed notes in English" in (
        user_prompt
    )
    # Two dispositions per note, closed discard reasons, no invented edit.
    assert (
        'declared in "discarded_notes" with one of the reasons already_in_bank, '
        "superseded, obsolete, no_bank_value — never both"
    ) in user_prompt
    assert "when in doubt, integrate" in user_prompt
    assert "NEVER invent an edit" in user_prompt
    assert '"discarded_notes": [' in user_prompt

    # Prompt language is the service's; operator-owned inputs stay verbatim.
    assert "# Règles exactes" in user_prompt
    assert "Synthèse historique exacte." in user_prompt
    assert "Décision source exacte avec `memory_id`." in user_prompt
    assert "## Focus Actuel" in user_prompt


def test_first_consolidation_prompt_is_english() -> None:
    messages = _service()._build_prompt(
        space_id="language-contract",
        rules="# Exact rules",
        synthesis="",
        notes=[
            {
                "key": (
                    "language-contract/live/"
                    "20260801T000000_agent-a_decision_11111111.md"
                ),
                "content": "Exact source note.",
            }
        ],
        bank_files=[],
    )
    user_prompt = messages[1]["content"]
    assert "No bank files — this is the first consolidation." in user_prompt
    assert "Aucun fichier bank — première consolidation." not in user_prompt


def test_corrective_completion_turns_share_the_conversation_language() -> None:
    """The corrective turn must be in the same
    language as the first completion. With a single English prompt set this
    holds by construction; pin it so a future prompt cannot reintroduce a split."""
    messages = _main_messages()
    form_fault_turn = _corrective_messages(
        messages,
        {"file_edits": [], "discarded_notes": [], "synthesis": "x"},
        [{"reason": "normal_notes_unclassified", "missing_notes": [1]}],
    )[-1]["content"]
    completion_fault_turn = _corrective_messages(
        messages, None, [], completion_fault="invalid_normal_consolidation_json"
    )[-1]["content"]

    assert form_fault_turn.startswith("Your previous plan was refused")
    assert "Notes without disposition: 1." in form_fault_turn
    assert completion_fault_turn.startswith("Your previous answer could not be used")
    for turn in (form_fault_turn, completion_fault_turn):
        assert "replaces the previous plan entirely" in turn or "nothing else" in turn
        assert "Votre" not in turn and "précédent" not in turn


async def test_duplicate_section_merge_prompt_is_english() -> None:
    service = _service()

    assert await service._merge_sections_via_llm("## Status", ["old", "new"]) == (
        "merged"
    )
    prompt = service._complete_chat.calls[0]["messages"][0]["content"]
    assert "Merge these versions into ONE coherent version." in prompt
    assert "Fusionne ces versions en UNE SEULE version cohérente." not in prompt


async def test_compaction_prompt_is_english() -> None:
    service = _service()
    source = "# Bank\n\n## Details\n" + "verbose detail " * 30
    service._complete_chat.text = "## Details\ncondensed"

    assert await service._compact_single_file(
        "activeContext.md", source, 100, "# Rules"
    ) == "# Bank\n\n## Details\ncondensed"
    prompt = "\n".join(
        message["content"]
        for message in service._complete_chat.calls[0]["messages"]
    )
    assert "Return concise English Markdown" in prompt
    assert "Fusionne les informations redondantes" not in prompt
    assert service._complete_chat.calls[0]["retry_policy"] == "none"
