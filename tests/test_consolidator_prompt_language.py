"""Language-selection contract for every server-owned consolidator prompt."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from hivemind_inference.records import ChatResult
from live_mem.core.consolidator import (
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_ENGLISH,
    SYSTEM_PROMPT_FRENCH,
    ConsolidatorService,
)


@dataclass
class RecordingCompletion:
    """Offline completion seam that records the exact prompt sent to the model."""

    text: str = "merged"
    calls: list[dict] = field(default_factory=list)

    async def __call__(self, messages, output_budget):
        self.calls.append({"messages": messages, "output_budget": output_budget})
        return ChatResult(
            text=self.text,
            configured_model="test-model",
            model_evidence="configured_only",
            finish_reason="stop",
        )


def _service(*, legacy_french: bool) -> ConsolidatorService:
    service = object.__new__(ConsolidatorService)
    service._legacy_french_prompts = legacy_french
    service._complete_chat = RecordingCompletion()
    return service


def _main_messages(*, legacy_french: bool) -> list[dict]:
    service = _service(legacy_french=legacy_french)
    return service._build_prompt(
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


@pytest.mark.parametrize("legacy_french", [False, True])
def test_service_snapshots_the_configured_compatibility_mode(
    legacy_french: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from live_mem.core import consolidator as consolidator_module
    from live_mem.core import inference_runtime

    settings = SimpleNamespace(
        consolidation_timeout=600,
        consolidation_max_notes=200,
        consolidation_batch_size=5,
        consolidation_legacy_french_prompts=legacy_french,
        consolidation_cooldown_seconds=60,
        compact_threshold=0.6,
        bank_file_max_size=15360,
        consolidation_validation_enabled=False,
        consolidation_validation_max_examples=20,
    )
    runtime = SimpleNamespace(config=SimpleNamespace(chat=None))
    monkeypatch.setattr(consolidator_module, "get_settings", lambda: settings)
    monkeypatch.setattr(inference_runtime, "get_inference_runtime", lambda: runtime)

    assert ConsolidatorService()._legacy_french_prompts is legacy_french


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
    from live_mem.core import consolidator as consolidator_module
    from live_mem.core import inference_runtime

    settings = SimpleNamespace(
        consolidation_timeout=600,
        consolidation_max_notes=200,
        consolidation_batch_size=5,
        consolidation_legacy_french_prompts=False,
        consolidation_cooldown_seconds=60,
        compact_threshold=0.6,
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


def test_english_default_main_prompt_preserves_source_material_verbatim() -> None:
    messages = _main_messages(legacy_french=False)
    system_prompt, user_prompt = [message["content"] for message in messages]

    assert system_prompt == SYSTEM_PROMPT_ENGLISH
    assert "Tu es un assistant spécialisé" not in system_prompt
    assert '=== RULES FOR SPACE "language-contract" ===' in user_prompt
    assert "=== PREVIOUS SYNTHESIS ===" in user_prompt
    assert "=== LIVE NOTES TO INTEGRATE (1 notes) ===" in user_prompt
    assert "[agent=agent-a, category=decision]" in user_prompt
    assert "SYNTHÈSE PRÉCÉDENTE" not in user_prompt
    assert "catégorie=decision" not in user_prompt
    assert '"content": "New section content..."' in user_prompt
    assert "The residual synthesis must summarize the processed notes in English" in (
        user_prompt
    )

    # Prompt language changes; operator-owned inputs do not.
    assert "# Règles exactes" in user_prompt
    assert "Synthèse historique exacte." in user_prompt
    assert "Décision source exacte avec `memory_id`." in user_prompt
    assert "## Focus Actuel" in user_prompt


def test_legacy_flag_preserves_the_historical_french_main_prompts() -> None:
    messages = _main_messages(legacy_french=True)
    system_prompt, user_prompt = [message["content"] for message in messages]

    assert system_prompt == SYSTEM_PROMPT_FRENCH
    assert SYSTEM_PROMPT_FRENCH.startswith("Tu es un assistant spécialisé")
    assert "[inféré]" in SYSTEM_PROMPT_FRENCH
    assert '=== RULES DE L\'ESPACE "language-contract" ===' in user_prompt
    assert "=== RULES FOR SPACE" not in user_prompt
    assert "=== SYNTHÈSE PRÉCÉDENTE ===" in user_prompt
    assert "[agent=agent-a, catégorie=decision]" in user_prompt
    assert '"content": "Nouveau contenu de la section..."' in user_prompt
    assert "La synthèse résiduelle doit résumer les notes traitées" in user_prompt


@pytest.mark.parametrize(
    ("legacy_french", "required", "forbidden"),
    [
        (
            False,
            "No bank files — this is the first consolidation.",
            "Aucun fichier bank — première consolidation.",
        ),
        (
            True,
            "Aucun fichier bank — première consolidation.",
            "No bank files — this is the first consolidation.",
        ),
    ],
)
def test_first_consolidation_uses_the_selected_prompt_language(
    legacy_french: bool,
    required: str,
    forbidden: str,
) -> None:
    service = _service(legacy_french=legacy_french)
    messages = service._build_prompt(
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
    assert required in user_prompt
    assert forbidden not in user_prompt


@pytest.mark.parametrize(
    ("legacy_french", "required", "forbidden"),
    [
        (
            False,
            "Merge these versions into ONE coherent version.",
            "Fusionne ces versions en UNE SEULE version cohérente.",
        ),
        (
            True,
            "Fusionne ces versions en UNE SEULE version cohérente.",
            "Merge these versions into ONE coherent version.",
        ),
    ],
)
async def test_duplicate_section_merge_uses_the_selected_prompt_language(
    legacy_french: bool,
    required: str,
    forbidden: str,
) -> None:
    service = _service(legacy_french=legacy_french)

    assert await service._merge_sections_via_llm("## Status", ["old", "new"]) == (
        "merged"
    )
    prompt = service._complete_chat.calls[0]["messages"][0]["content"]
    assert required in prompt
    assert forbidden not in prompt


@pytest.mark.parametrize(
    ("legacy_french", "required", "forbidden"),
    [
        (
            False,
            "Merge redundant information",
            "Fusionne les informations redondantes",
        ),
        (
            True,
            "Fusionne les informations redondantes",
            "Merge redundant information",
        ),
    ],
)
async def test_compaction_uses_the_selected_prompt_language(
    legacy_french: bool,
    required: str,
    forbidden: str,
) -> None:
    service = _service(legacy_french=legacy_french)
    service._complete_chat.text = "# Compacted"

    assert await service._compact_single_file(
        "activeContext.md", "content", 100, "# Rules"
    ) == "# Compacted"
    prompt = service._complete_chat.calls[0]["messages"][0]["content"]
    assert required in prompt
    assert forbidden not in prompt
