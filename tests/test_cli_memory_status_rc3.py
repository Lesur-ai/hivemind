"""User-facing CLI outcomes for the RC3 SHORT → MID → LONG journey."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cli import commands, shell  # noqa: E402


class FakeClient:
    replies = {}
    calls = []

    def __init__(self, *_args, **_kwargs):
        pass

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "long_ingest_list" and arguments.get("archive") is True:
            return self.replies[(name, True, arguments["status"])]
        if (name, arguments.get("status")) in self.replies:
            return self.replies[(name, arguments["status"])]
        return self.replies[name]


def _run(monkeypatch, args, replies):
    FakeClient.replies = replies
    FakeClient.calls = []
    monkeypatch.setattr(commands, "MCPClient", FakeClient)
    return CliRunner().invoke(commands.cli, args)


def test_consolidate_ack_is_not_a_completed_run(monkeypatch):
    result = _run(monkeypatch, ["bank", "consolidate", "alpha"], {
        "bank_consolidate": {
            "status": "queued", "job_id": "consol_1", "space_id": "alpha",
            "queue_position": 2, "agent": "alice", "requested_by": "alice",
        },
    })
    assert result.exit_code == 0
    assert "queued" in result.output and "consol_1" in result.output
    assert "bank consolidation-status consol_1" in result.output
    assert "Consolidation complete" not in result.output
    assert "Notes processed: 0" not in result.output


def test_terminal_job_renders_compaction_outcome_and_exit_status(monkeypatch):
    job = {
        "status": "succeeded", "job_id": "consol_1", "space_id": "alpha",
        "agent": "", "requested_by": "admin", "queue_position": 0,
        "result": {"notes_total": 3, "notes_processed": 3,
                   "auto_compaction": {"status": "not_needed", "files_over_limit": 0}},
    }
    result = _run(monkeypatch, ["bank", "consolidation-status", "consol_1"], {
        "bank_consolidation_status": job,
    })
    assert result.exit_code == 0
    assert "succeeded" in result.output
    assert "No MID file exceeded" in result.output
    assert "no new LONG capture" in result.output

    job["status"] = "failed"
    job["error"] = "Consolidation stopped safely."
    failed = _run(monkeypatch, ["bank", "consolidation-status", "consol_1"], {
        "bank_consolidation_status": job,
    })
    assert failed.exit_code == 1
    assert "Consolidation Job — failed" in failed.output


def test_mcp_failure_is_nonzero_even_in_json_mode(monkeypatch):
    result = _run(monkeypatch, ["graph", "status", "alpha", "--json"], {
        "graph_status": {"status": "error", "message": "Synthetic failure"},
    })
    assert result.exit_code == 1
    assert json.loads(result.output) == {"status": "error", "message": "Synthetic failure"}


def _space_replies(pending=0):
    return {
        "space_info": {
            "status": "ok", "space_id": "alpha",
            "live": {"notes_count": 0},
            "bank": {"files_count": 6, "total_size": 33875},
            "last_consolidation": "2026-09-30T16:29:38Z",
            "consolidation_queue": {"latest_jobs": [{
                "status": "succeeded", "job_id": "consol_1", "space_id": "alpha",
                "finished_at": "2026-09-30T16:29:38Z",
                "result": {"notes_processed": 3, "auto_compaction": {
                    "status": "not_needed", "files_over_limit": 0,
                }},
            }]},
        },
        "graph_status": {
            "status": "ok", "space_id": "alpha", "connected": True, "reachable": True,
            "mid_automation": {"compaction_enabled": True, "archive_enabled": True,
                               "file_threshold_bytes": 35000},
            "mid_archive_projection": {"pending": pending, "error": None},
            "graph_stats": {"document_count": 7},
        },
        ("long_ingest_list", "running"): {"status": "ok", "total": 0, "jobs": []},
        ("long_ingest_list", "queued"): {"status": "ok", "total": 0, "jobs": []},
        ("long_ingest_list", True, "running"): {"status": "ok", "total": 0, "jobs": []},
        ("long_ingest_list", True, "queued"): {"status": "ok", "total": 0, "jobs": []},
    }


def test_space_status_shows_archive_embedder_and_explicit_reindex_action(monkeypatch):
    replies = _space_replies()
    replies["graph_status"]["mid_archive_index"] = {
        "status": "ok", "embedding_collection": {"state": "reindex_required", "reason": "static_profile_mismatch"},
        "embedding_identity": {
            "persisted": {"model": "qwen3-embedding:0.6b", "dimensions": 1024},
            "configured": {"model": "replacement", "dimensions": 1024},
        },
    }
    human = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert human.exit_code == 0
    assert "qwen3-embedding:0.6b" in human.output and "1024" in human.output
    assert "replacement" in human.output
    assert "Explicit reindex required" in human.output
    json_result = _run(monkeypatch, ["space", "status", "alpha", "--json"], replies)
    assert json_result.exit_code == 0
    assert json.loads(json_result.output)["long_status"]["mid_archive_index"] == replies["graph_status"]["mid_archive_index"]


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "cancelled", "skipped", "changed_skipped"])
def test_cli_archive_job_detail_shows_real_step_and_routes_explicitly(monkeypatch, status):
    # The actual LONG API returns the raw Graph job, without an outer job wrapper.
    response = {"job_id": "ing-1", "status": status, "current_step": "llm_extract",
                "progress_percent": 42, "started_at": "2026-10-05T14:00:00Z"}
    if status == "failed":
        response["error"] = "inference chat failure: category=unavailable"
    human = _run(monkeypatch, ["graph", "job", "alpha", "ing-1", "--archive"], {"long_ingest_status": response})
    assert human.exit_code == (1 if status in {"failed", "cancelled", "changed_skipped"} else 0)
    assert "llm_extract" in human.output and "42%" in human.output and "ing-1" in human.output
    assert status in human.output
    if status == "failed":
        assert response["error"] in human.output
    assert FakeClient.calls == [("long_ingest_status", {"space_id": "alpha", "job_id": "ing-1", "archive": True})]
    raw = _run(monkeypatch, ["graph", "job", "alpha", "ing-1", "--archive", "--json"], {"long_ingest_status": response})
    assert raw.exit_code == (1 if status in {"failed", "cancelled", "changed_skipped"} else 0)
    assert json.loads(raw.output) == response


@pytest.mark.parametrize("status", ["running", "succeeded", "failed"])
def test_shell_archive_job_uses_same_route_and_renderer(monkeypatch, capsys, status):
    FakeClient.calls = []
    job = {"job_id": "ing-2", "status": status, "current_step": "llm_extract"}
    if status == "failed":
        job["error"] = "invalid_response"
    FakeClient.replies = {"long_ingest_status": job}
    asyncio.run(shell.dispatch(FakeClient(), "graph job alpha ing-2 --archive", False))
    output = capsys.readouterr().out
    assert status in output and "ing-2" in output and "llm_extract" in output
    if status == "failed":
        assert "invalid_response" in output
    assert "Progress:" not in output
    assert FakeClient.calls == [("long_ingest_status", {"space_id": "alpha", "job_id": "ing-2", "archive": True})]


def test_archive_job_api_rejection_does_not_invent_job_or_progress(monkeypatch, capsys):
    rejected = {"status": "error", "message": "Access denied"}
    result = _run(monkeypatch, ["graph", "job", "alpha", "ing-2", "--archive"], {"long_ingest_status": rejected})
    assert result.exit_code == 1
    assert "Access denied" in result.output
    assert "Job:" not in result.output and "Progress:" not in result.output
    FakeClient.replies = {"long_ingest_status": rejected}
    asyncio.run(shell.dispatch(FakeClient(), "graph job alpha ing-2 --archive", False))
    output = capsys.readouterr().out
    assert "Access denied" in output
    assert "Job:" not in output and "Progress:" not in output


def test_space_status_explains_three_tiers_without_claiming_index_completeness(monkeypatch):
    result = _run(monkeypatch, ["space", "status", "alpha"], _space_replies())
    assert result.exit_code == 0
    assert "SHORT" in result.output and "MID" in result.output and "LONG" in result.output
    assert "3 notes" in result.output
    assert "No MID file exceeded" in result.output
    assert "0 captures pending" in result.output
    assert "not proof" in result.output
    assert "7 documents" not in result.output  # documentary graph ≠ archive receipts
    assert FakeClient.calls == [
        ("space_info", {"space_id": "alpha"}),
        ("graph_status", {"space_id": "alpha"}),
        ("long_ingest_list", {"space_id": "alpha", "status": "running", "limit": 10}),
        ("long_ingest_list", {"space_id": "alpha", "status": "queued", "limit": 10}),
    ]


def test_shell_space_status_uses_the_same_read_only_sources(monkeypatch, capsys):
    replies = _space_replies(pending=2)
    FakeClient.replies = replies
    FakeClient.calls = []
    asyncio.run(shell.dispatch(FakeClient(), "space status alpha", False))
    output = capsys.readouterr().out
    assert "SHORT" in output and "MID" in output and "LONG" in output
    assert "2 captures pending" in output
    assert FakeClient.calls == [
        ("space_info", {"space_id": "alpha"}),
        ("graph_status", {"space_id": "alpha"}),
        ("long_ingest_list", {"space_id": "alpha", "status": "running", "limit": 10}),
        ("long_ingest_list", {"space_id": "alpha", "status": "queued", "limit": 10}),
        ("long_ingest_list", {"space_id": "alpha", "status": "running", "limit": 10, "archive": True}),
        ("long_ingest_list", {"space_id": "alpha", "status": "queued", "limit": 10, "archive": True}),
    ]


def test_space_status_reports_disabled_transfer_and_projection_error(monkeypatch):
    replies = _space_replies(pending=2)
    replies["graph_status"]["mid_automation"]["archive_enabled"] = False
    replies["graph_status"]["mid_archive_projection"]["error"] = "invalid_record"
    replies["space_info"]["consolidation_queue"]["latest_jobs"][0]["result"]["auto_compaction"] = {
        "status": "disabled",
    }
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "MID compaction disabled" in result.output
    assert "Transfer disabled" in result.output
    assert "2 captures pending" in result.output
    assert "Indexing problem: invalid_record" in result.output


def test_space_status_partial_read_fails_without_hiding_short_mid(monkeypatch):
    replies = _space_replies()
    replies["graph_status"] = {"status": "error", "message": "Graph unavailable"}
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 1
    assert "SHORT" in result.output and "MID" in result.output
    assert "LONG" in result.output and "unavailable" in result.output

    json_result = _run(monkeypatch, ["space", "status", "alpha", "--json"], replies)
    assert json_result.exit_code == 1
    payload = json.loads(json_result.output)
    assert payload["status"] == "partial"
    assert payload["space_info"]["status"] == "ok"
    assert payload["long_status"]["message"] == "Graph unavailable"


def test_space_status_shows_running_phases_and_long_ingestion_progress(monkeypatch):
    replies = _space_replies(pending=1)
    latest = replies["space_info"]["consolidation_queue"]["latest_jobs"][0]
    latest.update(status="running", progress={
        "phase": "compacting", "notes_total": 8, "notes_done": 8,
        "batches_total": 2, "batches_done": 2,
    }, result=None)
    replies[("long_ingest_list", "running")] = {"status": "ok", "total": 1, "jobs": [{
        "job_id": "ingest_1", "status": "running", "current_step": "embedding",
        "progress_percent": 42,
    }]}
    replies[("long_ingest_list", "queued")] = {"status": "ok", "total": 2, "jobs": [{
        "job_id": "ingest_2", "status": "queued", "current_step": "queued",
        "progress_percent": 0,
    }]}
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "compacting" in result.output
    assert "8/8 notes" in result.output
    assert "2/2 batches" in result.output
    assert "1 running" in result.output and "2 queued" in result.output
    assert "ingest_1" in result.output and "embedding" in result.output
    assert "42%" in result.output
    assert "ingest_2" in result.output


def test_space_status_does_not_invent_long_job_progress(monkeypatch):
    replies = _space_replies()
    replies[("long_ingest_list", "running")] = {"status": "error", "message": "Queue unavailable"}
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 1
    assert "Ingestion jobs unavailable" in result.output
    assert "42%" not in result.output


def test_space_status_prefers_running_consolidation_over_newer_queued_job(monkeypatch):
    replies = _space_replies()
    lane = replies["space_info"]["consolidation_queue"]
    lane["latest_jobs"][0].update(status="queued", job_id="consol_new", result=None)
    lane["running_job"] = {
        "status": "running", "job_id": "consol_active", "agent": "alice",
        "started_at": "2026-09-30T16:00:00Z",
        "progress": {"phase": "compacting"},
    }
    lane["queued_count"] = 1
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "running" in result.output and "compacting" in result.output
    assert "bank consolidation-status consol_active" in result.output
    assert "bank consolidation-status consol_new" not in result.output


def test_space_status_keeps_failed_compaction_separate_from_consolidation(monkeypatch):
    replies = _space_replies()
    result_data = replies["space_info"]["consolidation_queue"]["latest_jobs"][0]["result"]
    result_data["auto_compaction"] = {"status": "error", "reason": "llm_invalid_response"}
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "Last result: succeeded" in result.output
    assert "MID compaction error; consolidation is separate" in result.output


def test_archive_capture_is_not_confused_with_primary_document_jobs(monkeypatch):
    replies = _space_replies(pending=1)
    replies["graph_status"]["mid_archive_projection"].update(
        oldest_at="2026-09-30T14:00:00Z", oldest_age_seconds=7200,
    )
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "1 capture pending indexing" in result.output
    assert "oldest" in result.output.lower()
    assert "Document ingestion" in result.output
    assert "Capture indexing" in result.output


def test_space_status_shows_archive_job_stage_separately_from_documents(monkeypatch):
    replies = _space_replies(pending=1)
    replies[("long_ingest_list", True, "running")] = {
        "status": "ok", "memory_id": "mid_archive", "total": 1,
        "jobs": [{"job_id": "capture_job", "status": "running",
                  "current_step": "extracting", "progress_percent": 40}],
    }
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "Capture indexing: 1 running · 0 queued" in result.output
    assert "capture_job · extracting · 40%" in result.output
    assert "Document ingestion: 0 running · 0 queued" in result.output
    assert FakeClient.calls[-2:] == [
        ("long_ingest_list", {"space_id": "alpha", "status": "running", "limit": 10, "archive": True}),
        ("long_ingest_list", {"space_id": "alpha", "status": "queued", "limit": 10, "archive": True}),
    ]


def test_space_status_distinguishes_archive_not_ready_from_read_failure(monkeypatch):
    replies = _space_replies(pending=1)
    replies[("long_ingest_list", True, "running")] = {
        "status": "error", "reason": "archive_not_configured",
        "message": "No archive destination is configured.",
    }
    waiting = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert waiting.exit_code == 0
    assert "archive not yet created" in waiting.output
    assert not any(args.get("archive") and args.get("status") == "queued"
                   for _, args in FakeClient.calls)

    replies[("long_ingest_list", True, "running")] = {
        "status": "error", "reason": "archive_unavailable", "message": "Archive unreachable",
    }
    failed = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert failed.exit_code == 1
    assert "Archive unreachable" in failed.output
    raw = _run(monkeypatch, ["space", "status", "alpha", "--json"], replies)
    assert raw.exit_code == 1
    assert json.loads(raw.output)["status"] == "partial"


def test_unbound_space_with_pending_capture_is_waiting_not_a_graph_connect_error(monkeypatch):
    replies = _space_replies(pending=1)
    replies["graph_status"].update(connected=False, bound=False, embedded=True)
    replies[("long_ingest_list", True, "running")] = {
        "status": "error", "reason": "archive_not_configured",
        "message": "No archive destination is configured.",
    }
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "archive not yet created" in result.output
    assert "LONG binds automatically" in result.output
    assert "graph_connect" not in result.output
    assert not any(name == "long_ingest_list" and not args.get("archive")
                   for name, args in FakeClient.calls)
    raw = _run(monkeypatch, ["space", "status", "alpha", "--json"], replies)
    assert raw.exit_code == 0
    assert json.loads(raw.output)["status"] == "ok"


def test_known_graph_outage_skips_archive_reads_and_fails_fast(monkeypatch):
    replies = _space_replies(pending=1)
    replies["graph_status"]["reachable"] = False
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 1
    assert "Graph service unreachable" in result.output
    assert not any(name == "long_ingest_list" for name, _ in FakeClient.calls)
    raw = _run(monkeypatch, ["space", "status", "alpha", "--json"], replies)
    assert raw.exit_code == 1
    assert json.loads(raw.output)["status"] == "partial"
    assert not any(name == "long_ingest_list" for name, _ in FakeClient.calls)


def test_shell_known_graph_outage_skips_archive_reads(monkeypatch, capsys):
    replies = _space_replies(pending=1)
    replies["graph_status"]["reachable"] = False
    FakeClient.replies = replies
    FakeClient.calls = []
    asyncio.run(shell.dispatch(FakeClient(), "space status alpha", True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "partial"
    assert payload["archive_ingest_running"] is None
    assert not any(name == "long_ingest_list" for name, _ in FakeClient.calls)


def test_failed_compaction_requires_recovery_and_does_not_claim_retention(monkeypatch):
    replies = _space_replies()
    maintenance = replies["space_info"]["consolidation_queue"]["latest_jobs"][0]["result"]["auto_compaction"]
    maintenance.update(status="error", reason="hivemind_state_corrupt", recovery_required=True)
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 1
    assert "hivemind_state_corrupt" in result.output
    assert "RECOVERY REQUIRED" in result.output
    assert "bank consolidation-status consol_1" in result.output
    assert "Original capture retained" not in result.output
    json_result = _run(monkeypatch, ["space", "status", "alpha", "--json"], replies)
    assert json_result.exit_code == 1
    assert json.loads(json_result.output)["recovery_required"] is True

    maintenance.update(status="cancelled", reason="cancelled", rollback_outcome="unverified")
    cancelled = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert cancelled.exit_code == 1
    assert "Rollback: unverified" in cancelled.output


def test_recovery_required_not_applicable_is_visible_in_status_and_receipt(monkeypatch):
    replies = _space_replies()
    job = replies["space_info"]["consolidation_queue"]["latest_jobs"][0]
    job["result"]["auto_compaction"] = {
        "status": "not_applicable", "reason": "direct_local_route_required",
        "recovery_required": True,
    }
    overview = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert overview.exit_code == 1
    assert "RECOVERY REQUIRED" in overview.output
    assert "direct_local_route_required" in overview.output
    assert "bank consolidation-status consol_1" in overview.output

    receipt = _run(monkeypatch, ["bank", "consolidation-status", "consol_1"], {
        "bank_consolidation_status": job,
    })
    assert receipt.exit_code == 1
    assert "RECOVERY REQUIRED" in receipt.output
    assert "direct_local_route_required" in receipt.output
    receipt_json = _run(monkeypatch, ["bank", "consolidation-status", "consol_1", "--json"], {
        "bank_consolidation_status": job,
    })
    assert receipt_json.exit_code == 1
    assert json.loads(receipt_json.output)["result"]["auto_compaction"]["recovery_required"] is True


def test_compact_bank_failure_reason_phase_and_receipt_are_visible(monkeypatch):
    replies = _space_replies()
    job = replies["space_info"]["consolidation_queue"]["latest_jobs"][0]
    job["result"]["auto_compaction"] = {
        "status": "error", "failure_reason": "compaction_apply_reverted",
        "failed_phase": "apply",
    }
    overview = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert overview.exit_code == 0  # consolidation succeeded; compaction is separately reported
    assert "compaction_apply_reverted" in overview.output
    assert "Phase: apply" in overview.output
    assert "bank consolidation-status consol_1" in overview.output


def test_capture_retention_claim_requires_preimage_receipt(monkeypatch):
    replies = _space_replies()
    maintenance = replies["space_info"]["consolidation_queue"]["latest_jobs"][0]["result"]["auto_compaction"]
    maintenance.update(status="ok", files_over_limit=1)
    without = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert "Original capture retained" not in without.output
    maintenance["preimage_id"] = "capture_1"
    with_receipt = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert "Original capture retained" in with_receipt.output


def test_queued_retry_does_not_hide_last_failure(monkeypatch):
    replies = _space_replies()
    lane = replies["space_info"]["consolidation_queue"]
    queued = {"status": "queued", "job_id": "consol_retry", "agent": "bob",
              "requested_at": "2026-09-30T17:00:00Z", "progress": {"phase": "queued"}}
    failed = {"status": "failed", "job_id": "consol_failed", "agent": "bob",
              "finished_at": "2026-09-30T16:59:00Z", "error": "LLM timeout"}
    lane.update(running_job=None, queued_jobs=[queued], latest_jobs=[queued, failed])
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "Current job: queued" in result.output
    assert "Last result: failed" in result.output
    assert "LLM timeout" in result.output


def test_ten_active_jobs_do_not_imply_no_prior_terminal_result(monkeypatch):
    replies = _space_replies()
    queue = replies["space_info"]["consolidation_queue"]
    waiting = [{"status": "queued", "job_id": f"job_{index}", "agent": f"agent_{index}"}
               for index in range(10)]
    queue.update(running_job=None, queued_jobs=waiting, latest_jobs=waiting)
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "Last result: not among the 10 most recent jobs" in result.output


def test_unknown_backlog_and_malformed_primary_job_total_are_honest(monkeypatch):
    replies = _space_replies()
    replies["graph_status"]["mid_archive_projection"].update(
        pending=0, error="projection_unavailable",
    )
    replies[("long_ingest_list", "running")]["total"] = "[/]"
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "capture backlog unknown" in result.output
    assert "Document ingestion: unknown running" in result.output


def test_embedded_unbound_message_and_shell_error_follow_server_state(monkeypatch, capsys):
    replies = _space_replies()
    replies["graph_status"] = {"status": "ok", "connected": False,
                               "embedded": True, "bound": False,
                               "mid_automation": {"archive_enabled": True},
                               "mid_archive_projection": {"pending": 0}}
    result = _run(monkeypatch, ["space", "status", "alpha"], replies)
    assert result.exit_code == 0
    assert "Waiting for first ingestion" in result.output

    FakeClient.replies = {"bank_consolidation_status": {
        "status": "error", "message": "Access denied",
    }}
    asyncio.run(shell.dispatch(FakeClient(), "bank consolidation-status job-1", False))
    output = capsys.readouterr().out
    assert "Access denied" in output
    assert "Consolidation Job" not in output


@pytest.mark.parametrize("command", [["space", "status", "alpha"], ["graph", "status", "alpha"]])
@pytest.mark.parametrize("category", ["invalid_output", "inference_timeout"])
def test_archive_pause_is_visible_and_retry_schedules_once(monkeypatch, command, category):
    replies = _space_replies(pending=1)
    replies['graph_status']['mid_archive_projection'].update(
        blocked=1, error=category, rejection_reason='malformed_json' if category == 'invalid_output' else None, next_attempt_at=None)
    shown = _run(monkeypatch, command, replies)
    assert shown.exit_code == 0 and 'PAUSED' in shown.output  # Derived indexing is not MID recovery.
    assert category in shown.output and 'bank archive-retry' in shown.output
    if category == 'invalid_output':
        assert 'malformed_json' in shown.output
    else:
        assert 'after repeated failures' in shown.output
        assert 'invalid ontology output' not in shown.output
    retry = _run(monkeypatch, ['bank', 'archive-retry', 'alpha', 'alpha/capture', '--json'], {
        'mid_archive_retry': {'status':'ok', 'resumed':True, 'message':'Archive retry scheduled.'}})
    assert retry.exit_code == 0 and json.loads(retry.output)['resumed'] is True
    assert FakeClient.calls == [('mid_archive_retry', {'space_id':'alpha', 'preimage_id':'alpha/capture'})]


@pytest.mark.parametrize("command", [["space", "status", "alpha"], ["graph", "status", "alpha"]])
@pytest.mark.parametrize("deadline", [1791385200, 1e100])
def test_archive_retry_deadline_display_is_local_and_does_not_crash(monkeypatch, command, deadline):
    replies = _space_replies(pending=1)
    replies["graph_status"]["mid_archive_projection"].update(
        error="projection_unavailable", next_attempt_at=deadline)
    shown = _run(monkeypatch, command, replies)
    assert shown.exit_code == 0 and "Next automatic retry" in shown.output
    if deadline == 1791385200:
        assert "UTC" in shown.output
    else:
        assert "1e+100" in shown.output
