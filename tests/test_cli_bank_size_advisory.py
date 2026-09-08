# -*- coding: utf-8 -*-
"""The CLI reports oversized bank files as an advisory, nothing more.

Compaction is a human decision (``bank_compact``): a consolidation never runs
it, so a job result never carries a compaction envelope. The CLI shows the
``bank_size_advisory`` list the server projected, literally and escaped, and
stays silent when there is none. Tested by EXECUTION of the display code.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cli import display as cli_display  # noqa: E402


def _render(job: dict) -> str:
    from rich.console import Console

    console = Console(record=True, width=200, no_color=True)
    original = cli_display.console
    cli_display.console = console
    try:
        cli_display.show_consolidation_job(job)
    finally:
        cli_display.console = original
    return console.export_text()


def _job(result: dict) -> dict:
    return {"status": "succeeded", "job_id": "consol_457h", "space_id": "demo-space", "result": result}


def test_a_succeeded_job_shows_the_bank_size_advisory():
    out = _render(_job({
        "status": "ok",
        "notes_processed": 3,
        "bank_size_advisory": [{"filename": "progress.md", "utf8_bytes": 42553, "max_size": 35000}],
    }))
    assert "Bank size advisory" in out
    assert "progress.md" in out and "42553" in out and "35000" in out
    assert "human decision" in out
    assert "refused" not in out.lower()


def test_an_ordinary_success_says_nothing_about_sizes():
    out = _render(_job({"status": "ok", "notes_processed": 3}))
    assert "advisory" not in out.lower()
    assert "compaction" not in out.lower()


@pytest.mark.parametrize(
    "items",
    [
        "not-a-list",
        [{"filename": 12, "utf8_bytes": 1, "max_size": 1}],
        [{"filename": "a.md", "utf8_bytes": "big", "max_size": 1}],
        [{"filename": "a.md", "utf8_bytes": 1}],
        [None, 3, "x"],
    ],
)
def test_malformed_server_values_are_skipped_never_crash(items):
    out = _render(_job({"status": "ok", "notes_processed": 1, "bank_size_advisory": items}))
    assert "Bank size advisory" not in out


def test_a_hostile_filename_is_displayed_literally():
    out = _render(_job({
        "status": "ok",
        "notes_processed": 1,
        "bank_size_advisory": [{"filename": "[bold red]x[/bold red].md", "utf8_bytes": 99999, "max_size": 35000}],
    }))
    assert "[bold red]x[/bold red].md" in out


def test_helper_lines_are_content_free_and_typed():
    lines = cli_display._bank_size_advisory_lines(
        [{"filename": "activeContext.md", "utf8_bytes": 48621, "max_size": 35000, "content": "SECRET"}]
    )
    assert lines == ["  - activeContext.md: 48621 UTF-8 bytes (advisory threshold 35000)"]
    assert cli_display._bank_size_advisory_lines(None) == []


def test_a_stale_compaction_envelope_from_an_old_server_renders_nothing():
    """The CLI has no compaction-advisory branch left; an old server's
    envelope is ignored rather than displayed as a refused compaction."""
    out = _render(_job({
        "status": "ok",
        "notes_processed": 1,
        "compaction_advisory": True,
        "compaction_advisory_reason": "compaction_prepare_failed",
        "preimage_id": "demo-space/2026-09-05T00-00-00-" + "a" * 32,
    }))
    assert "refused" not in out.lower() and "Compaction" not in out
    assert "demo-space/2026-09-05" not in out
