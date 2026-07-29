"""Regression checks for the English default product surfaces."""

from pathlib import Path

from live_mem.core.consolidator import SYSTEM_PROMPT


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_consolidation_prompt_defaults_to_english() -> None:
    assert SYSTEM_PROMPT.startswith("You maintain structured project Memory Banks.")
    assert "[inferred]" in SYSTEM_PROMPT
    assert "Tu es un assistant" not in SYSTEM_PROMPT


def test_operator_ui_uses_english_locale_and_language_tag() -> None:
    config = _read("src/live_mem/static/js/config.js")
    admin = _read("src/live_mem/static/js/admin-app.js")

    assert "fr-FR" not in config
    assert "lang=\"fr\"" not in admin
    assert "en-US" in config
    assert "lang=\"en\"" in admin


def test_graph_ui_defaults_to_english() -> None:
    graph_html = _read(
        "services/graph-memory/src/mcp_memory/static/graph.html"
    )
    graph_js = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            ROOT / "services/graph-memory/src/mcp_memory/static/js"
        ).glob("*.js")
    )

    assert '<html lang="en">' in graph_html
    assert '<html lang="fr">' not in graph_html
    assert "fr-FR" not in graph_js

    for misplaced_label in (
        "Se connecter",
        "-- Mémoire --",
        "Réflexion en cours",
        "Isoler le sujet",
        "Token invalide",
    ):
        assert misplaced_label not in graph_html
        assert misplaced_label not in graph_js


def test_graph_mcp_metadata_has_english_defaults() -> None:
    server = _read("services/graph-memory/src/mcp_memory/server.py")

    assert '@mcp.tool(description="Create an isolated graph-memory namespace.")' in server
    assert 'Field(description="Identifiant' not in server
    assert 'Field(description="ID de la mémoire' not in server
