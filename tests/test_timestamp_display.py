"""Local-time presentation guards for the admin console and human CLI."""

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from cli import display  # noqa: E402


@pytest.fixture
def paris_timezone():
    if not hasattr(time, "tzset"):
        pytest.skip("process timezone control is unavailable")
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Paris"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_admin_timestamp_helper_uses_browser_timezone_and_dst():
    env = os.environ.copy()
    env["TZ"] = "Europe/Paris"
    result = subprocess.run(
        [
            "node",
            str(ROOT / "tests/js/admin_timestamp_runtime.mjs"),
            str(ROOT / "src/live_mem/static/js/admin-app.js"),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "admin timestamp runtime assertions passed" in result.stdout


def test_admin_has_no_forced_utc_presentation():
    static = ROOT / "src/live_mem/static"
    app = (static / "js/admin-app.js").read_text(encoding="utf-8")
    access = (static / "js/admin/views-access.js").read_text(encoding="utf-8")
    operator = (static / "js/admin/views-operator.js").read_text(encoding="utf-8")
    css = (static / "css/admin.css").read_text(encoding="utf-8")

    assert "getUTC" not in app
    assert "unit-utc" not in app + access + css
    assert "unit-timezone" in app + access + css
    assert "renderTimestamp(b.timestamp)" in operator
    assert "renderTimestamp(data.cutoff_date)" in operator
    assert "renderTimestamp(s.oldest)" in operator


def test_cli_timestamp_helper_uses_process_timezone_and_keeps_unknown_values(
    paris_timezone,
):
    assert display._format_local_timestamp("2026-01-15T12:00:00Z") == (
        "2026-01-15 13:00:00 UTC+01:00"
    )
    assert display._format_local_timestamp("2026-07-15T12:00:00+00:00") == (
        "2026-07-15 14:00:00 UTC+02:00"
    )
    assert display._format_local_timestamp("2026-01-15T23:30:00") == (
        "2026-01-16 00:30:00 UTC+01:00"
    )
    assert display._format_local_timestamp("2026-01-15T12-00-00") == (
        "2026-01-15 13:00:00 UTC+01:00"
    )
    assert display._format_local_timestamp(
        "2026-01-15T12-00-00-" + "a" * 32
    ) == "2026-01-15 13:00:00 UTC+01:00"
    assert display._format_local_timestamp("20260115T120000") == (
        "2026-01-15 13:00:00 UTC+01:00"
    )
    assert display._format_local_timestamp("2026-01-15") == "2026-01-15"
    assert display._format_local_timestamp("not-a-timestamp") == "not-a-timestamp"
    assert display._format_local_timestamp(
        "2026-01-15T12-00-00-" + "A" * 32, date_only=True
    ) == "2026-01-15"


def test_cli_human_output_is_local_but_json_stays_canonical(
    paris_timezone, monkeypatch, capsys
):
    stream = io.StringIO()
    monkeypatch.setattr(
        display,
        "console",
        display.Console(file=stream, color_system=None, width=180),
    )
    display.show_backup_list(
        {
            "total": 1,
            "backups": [
                {
                    "backup_id": "alpha/2026-07-15T12-00-00-deadbeef",
                    "space_id": "alpha",
                    "timestamp": "2026-07-15T12-00-00",
                }
            ],
        }
    )
    output = stream.getvalue()
    assert "Timestamp (local)" in output
    assert "2026-07-15 14:00:00 UTC+02:00" in output
    assert "alpha/2026-07-15T12-00-00-deadbeef" in output

    display.show_json({"timestamp": "2026-07-15T12:00:00Z"})
    raw = json.loads(capsys.readouterr().out)
    assert raw == {"timestamp": "2026-07-15T12:00:00Z"}
