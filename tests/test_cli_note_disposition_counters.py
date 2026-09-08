# -*- coding: utf-8 -*-
"""The CLI shows the final counters of a consolidation job even when
it failed, and names the notes declared useless separately from the deleted ones.
Tested by execution of the Rich renderer, not by source pinning."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cli import display as cli_display  # noqa: E402


def _capture(render) -> str:
    from rich.console import Console

    console = Console(record=True, width=200, no_color=True)
    original = cli_display.console
    cli_display.console = console
    try:
        render()
    finally:
        cli_display.console = original
    return console.export_text()


FAILED_JOB = {
    "status": "failed",
    "job_id": "consol_457d",
    "space_id": "demo-space",
    "error": "Consolidation stopped at batch 2/3 (batch_llm_failed)",
    "result": {
        "status": "partial",
        "failure_reason": "batch_llm_failed",
        "failed_batch": 2,
        "notes_total": 6,
        "notes_processed": 2,
        "notes_discarded_count": 1,
        "notes_deleted": 2,
        "notes_remaining": 4,
    },
}


def test_a_failed_job_still_shows_its_final_counters():
    out = _capture(lambda: cli_display.show_consolidation_job(FAILED_JOB))
    assert "total=6" in out
    assert "processed=2" in out
    assert "useless=1" in out
    assert "deleted=2" in out
    assert "remaining=4" in out
    assert "Failed batch" in out and "2" in out
    assert "batch_llm_failed" in out


def test_a_job_without_counters_shows_none():
    job = {"status": "failed", "job_id": "x", "space_id": "s", "error": "boom", "result": {"status": "error"}}
    out = _capture(lambda: cli_display.show_consolidation_job(job))
    assert "Counters" not in out


def test_the_run_summary_names_useless_and_deleted_notes_separately():
    out = _capture(
        lambda: cli_display.show_consolidation_result(
            {
                "notes_total": 3,
                "notes_processed": 3,
                "notes_discarded_count": 2,
                "notes_deleted": 3,
                "notes_remaining": 0,
                "bank_files_created": 0,
                "bank_files_updated": 1,
                "synthesis_size": 12,
                "llm_tokens_used": 40,
                "duration_seconds": 1.5,
            }
        )
    )
    assert "Notes total" in out and "3" in out
    assert "Declared useless" in out
    assert "Notes deleted" in out
    assert "Notes remaining" in out
