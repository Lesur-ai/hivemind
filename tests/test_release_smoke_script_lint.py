# -*- coding: utf-8 -*-
"""P6-8 release-gate smoke-script lint (Codex P6-8 review #2).

The earlier ``scripts/release_smoke.sh`` shipped with three blocking bugs
that this lint guards against in CI:

1. ``HIVEMIND_BOOTSTRAP_TOKEN`` unset → script logged a warning and exited
   ``0`` (false-green). The fix-up fails closed with a clear error.
2. ``short_note`` was called with ``{"text": "..."}`` but the live
   ``live_note`` tool requires ``space_id`` + ``category`` + ``content``.
3. ``long_search`` was called but no such tool is registered on the live
   MCP surface (the long-tier tools are ``long_connect``, ``long_push``,
   ``long_status``, ``long_disconnect`` plus the historical ``long_query``).

This lint:

- ``bash -n``-validates the script (syntax check) and asserts the file is
  executable on the operator host's filesystem.
- Parses the ``mcp_call`` invocations via a focused regex and asserts every
  referenced tool is present in ``tests/fixtures/tool_surface.json`` (the
  P6-3 tool-surface lock).
- Asserts the script fails closed when ``HIVEMIND_BOOTSTRAP_TOKEN`` is
  unset (a textual check on the early ``if [ -z "${HIVEMIND_BOOTSTRAP_TOKEN:-}" ]
  then ... exit 1; fi`` block — the script must not exit 0 with a warning).
- Asserts the ``short_note`` call uses the required ``space_id``,
  ``category``, and ``content`` JSON keys.

Stdlib-only, offline, deterministic.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "release_smoke.sh"
_SURFACE = _REPO_ROOT / "tests" / "fixtures" / "tool_surface.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _script_text() -> str:
    assert _SCRIPT.exists(), f"scripts/release_smoke.sh not found at {_SCRIPT}"
    return _SCRIPT.read_text(encoding="utf-8")


def _tool_surface_names() -> set[str]:
    """Flat set of tool names from the tool-surface fixture.

    The fixture stores a JSON object whose values include the registered
    tool names; we walk the JSON and collect every string value that looks
    like a ``<tier>_<verb>`` identifier so the lint is resilient to minor
    schema changes (e.g. ``aliases`` vs ``tools`` keys).
    """
    assert _SURFACE.exists(), f"tool surface fixture not found at {_SURFACE}"
    data = json.loads(_SURFACE.read_text(encoding="utf-8"))

    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str) and re.fullmatch(r"[a-z]+_[a-z_]+", k):
                    names.add(k)
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            if re.fullmatch(r"[a-z]+_[a-z_]+", node):
                names.add(node)

    walk(data)
    return names


# Matches `mcp_call "<tool_name>" "<json args>"` invocations. The args
# string is a double-quoted shell literal whose JSON contents escape inner
# quotes as `\"`, so we accept either an escaped quote or any non-quote
# character up to the closing `"`.
_MCP_CALL_RE = re.compile(
    r'mcp_call\s+"(?P<tool>[a-z]+_[a-z_]+)"\s+"(?P<args>(?:\\.|[^"\\])*)"'
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_script_passes_bash_syntax_check() -> None:
    """``bash -n scripts/release_smoke.sh`` must succeed."""
    if shutil.which("bash") is None:
        pytest.skip("bash not available on this host")
    result = subprocess.run(
        ["bash", "-n", str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"bash -n {_SCRIPT.name} failed (rc={result.returncode}):\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_script_is_executable() -> None:
    """The smoke script must be marked executable for the operator."""
    import os

    assert os.access(_SCRIPT, os.X_OK), (
        f"{_SCRIPT} must be executable (chmod +x). The release-gate smoke "
        "is operator-run; an unexecutable script means the operator has "
        "to remember to `bash` it explicitly, which is brittle."
    )


def test_script_fails_closed_when_bootstrap_token_unset() -> None:
    """The script MUST exit non-zero when HIVEMIND_BOOTSTRAP_TOKEN is unset.

    Codex P6-8 review #2: the earlier code path logged a warning and
    exited 0, producing a false-green. We assert the textual pattern of
    the fail-closed block — the script must:
      - test ``-z "${HIVEMIND_BOOTSTRAP_TOKEN:-}"`` (or equivalent)
      - emit a clear error
      - ``exit 1`` (NOT ``exit 0`` and NOT a bare ``return``)
    """
    text = _script_text()

    # The fail-closed guard must be present.
    assert re.search(
        r'if\s+\[\s+-z\s+"\$\{HIVEMIND_BOOTSTRAP_TOKEN:-\}"\s+\]\s*;\s*then',
        text,
    ), (
        "scripts/release_smoke.sh must contain a fail-closed guard of the "
        "form `if [ -z \"${HIVEMIND_BOOTSTRAP_TOKEN:-}\" ]; then ... exit "
        "1; fi`. Codex P6-8 review #2: the earlier `exit 0` path produced "
        "a false-green."
    )

    # And the guard must `exit 1` (not 0).
    # Find the guard block and check the first `exit N` after it.
    m = re.search(
        r'if\s+\[\s+-z\s+"\$\{HIVEMIND_BOOTSTRAP_TOKEN:-\}"\s+\]\s*;\s*then'
        r'(?P<body>.*?)\bfi\b',
        text,
        flags=re.DOTALL,
    )
    assert m is not None, "fail-closed guard block did not match expected shape"
    body = m.group("body")
    assert "exit 1" in body, (
        "fail-closed guard MUST `exit 1` when HIVEMIND_BOOTSTRAP_TOKEN is "
        "unset. Found body:\n" + body
    )
    assert "exit 0" not in body, (
        "fail-closed guard MUST NOT `exit 0` (that was the prior false-green)."
    )

    # And the failure message must point operators at a REAL remediation
    # path. P7-5 round-1 Codex review: the previously referenced
    # `scripts/bootstrap_admin_token.sh` helper does not exist in the repo;
    # the real mechanisms are the compose-injected ADMIN_BOOTSTRAP_KEY and
    # the admin_create_token MCP tool.
    assert re.search(
        r"ADMIN_BOOTSTRAP_KEY|admin_create_token", body
    ), (
        "fail-closed guard SHOULD point operators at a real remediation "
        "path (ADMIN_BOOTSTRAP_KEY from .env or admin_create_token)."
    )
    assert not re.search(r"bootstrap[_-]admin[_-]token", body, re.IGNORECASE), (
        "fail-closed guard must not reference the nonexistent "
        "scripts/bootstrap_admin_token.sh helper (P7-5 round-1 finding)."
    )


def test_script_calls_only_registered_tools() -> None:
    """Every `mcp_call <tool>` must reference a tool in the surface fixture."""
    text = _script_text()
    surface = _tool_surface_names()

    calls = list(_MCP_CALL_RE.finditer(text))
    assert calls, (
        "No `mcp_call \"<tool>\" \"<args>\"` invocations found in "
        f"{_SCRIPT.name}. The smoke must exercise at least short_*, mid_* "
        "and long_*."
    )

    referenced_tools = {m.group("tool") for m in calls}
    unknown = referenced_tools - surface
    assert not unknown, (
        f"scripts/release_smoke.sh references unknown tool(s) "
        f"{sorted(unknown)} not present in tests/fixtures/tool_surface.json. "
        f"Surface tools available: {sorted(surface)}"
    )

    # Specifically: the script must exercise at least one short_*, mid_*
    # and long_* call (the three-tier round-trip required by ADR-0018
    # §Smoke).
    tiers = {t.split("_", 1)[0] for t in referenced_tools}
    for tier in ("short", "mid", "long"):
        assert tier in tiers, (
            f"scripts/release_smoke.sh must round-trip at least one "
            f"`{tier}_*` tool (ADR-0018 §Smoke). Referenced tools: "
            f"{sorted(referenced_tools)}"
        )


def test_short_note_call_uses_required_arguments() -> None:
    """`short_note` must pass `space_id`, `category`, and `content`.

    Codex P6-8 review #2: the earlier call used `{"text":"smoke note"}`
    which mismatches the live ``live_note`` signature (``space_id`` +
    ``category`` + ``content``).
    """
    text = _script_text()
    found = False
    for m in _MCP_CALL_RE.finditer(text):
        if m.group("tool") != "short_note":
            continue
        found = True
        args = m.group("args")
        for required_key in ("space_id", "category", "content"):
            assert f"\\\"{required_key}\\\"" in args or f'"{required_key}"' in args, (
                f"short_note invocation missing required JSON key "
                f"'{required_key}'. Found args: {args!r}"
            )
        # And it must NOT carry the obsolete `text` key.
        assert "\\\"text\\\"" not in args and '"text"' not in args, (
            f"short_note invocation still uses the obsolete 'text' key "
            f"(should be 'content'). Found args: {args!r}"
        )
    assert found, (
        "scripts/release_smoke.sh must include at least one "
        "`mcp_call \"short_note\" ...` invocation."
    )


def test_short_note_asserts_real_created_contract() -> None:
    """`short_note` success is `status == "created"`, never `"ok"` (P7-9).

    The real note-creation contract (``src/live_mem/core/live.py``) returns
    ``"created"``. The P7-5 script asserted ``!= "ok"`` and therefore failed
    against a healthy stack — same defect class as the ``space_create``
    contract finding from the P7-5 Codex round 1, missed on ``short_note``
    and never re-proven because the smoke is operator-run, not CI-run.
    This anchor makes a regression to the wrong contract RED at lint time.
    """
    import re as _re

    text = _script_text()
    assert _re.search(
        r'^if \[ "\$short_status" != "created" \]; then$', text, _re.MULTILINE
    ), (
        "scripts/release_smoke.sh must assert the REAL short_note contract "
        '(`if [ "$short_status" != "created" ]; then`) — a successful '
        "short_note/live_note returns 'created', never 'ok'."
    )
    assert '"$short_status" != "ok"' not in text, (
        "scripts/release_smoke.sh still checks short_status against 'ok' — "
        "that is the wrong contract (P7-9 gap 1); the real success status "
        "is 'created'."
    )


def test_long_tier_call_is_a_registered_long_tool() -> None:
    """Codex P6-8 review #2: `long_search` does not exist.

    The script must call one of the registered long-tier tools — typically
    ``long_status`` (lightweight, accepts the disabled-state shape per
    ADR-0010 / P6-5).
    """
    text = _script_text()
    surface = _tool_surface_names()
    long_tools_called = [
        m.group("tool")
        for m in _MCP_CALL_RE.finditer(text)
        if m.group("tool").startswith("long_")
    ]
    assert long_tools_called, "No long_* invocation found in release_smoke.sh"
    for tool in long_tools_called:
        assert tool in surface, (
            f"release_smoke.sh calls long-tier tool {tool!r} which is NOT "
            f"in tests/fixtures/tool_surface.json. Codex P6-8 review #2: "
            f"`long_search` is the historical example; pick one of "
            f"{sorted(t for t in surface if t.startswith('long_'))}"
        )
