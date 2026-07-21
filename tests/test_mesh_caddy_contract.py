"""Frozen edge contract for the default-on Project Mesh namespace."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CADDYFILE = ROOT / "waf" / "Caddyfile"


def _source() -> str:
    return CADDYFILE.read_text(encoding="utf-8")


def test_mesh_guard_is_default_on_and_separately_rate_limited() -> None:
    source = _source()
    assert source.count('expression `{env.HIVEMIND_MESH_ENABLED} == "true"`') == 2
    assert source.count("path /mesh/v1*") == 2
    mesh_zone = source.split("\t\tzone mesh {", 1)[1].split(
        "\t\tzone mcp {", 1
    )[0]
    assert "events 120" in mesh_zone
    assert "window 1m" in mesh_zone
    assert "key {remote_host}" in mesh_zone


def test_mesh_raw_body_limit_precedes_coraza_and_proxy() -> None:
    source = _source()
    assert "order coraza_waf after rate_limit" in source
    assert "order request_body before coraza_waf" in source
    mesh_handle = source.split("\thandle @mesh_enabled {", 1)[1].split("\n\t}", 1)[0]
    assert mesh_handle.count("max_size 256KiB") == 1
    assert mesh_handle.index("request_body {") < mesh_handle.index(
        "import hivemind_coraza"
    )
    assert mesh_handle.index("import hivemind_coraza") < mesh_handle.index(
        "reverse_proxy"
    )


def test_coraza_does_not_consume_the_guarded_mesh_stream() -> None:
    source = _source()
    # Coraza retains URI/header inspection. The downstream application/proxy
    # consumes the request_body-wrapped stream, preserving the typed 413 at +1.
    assert source.count('SecRule REQUEST_URI "@beginsWith /mesh/v1"') == 1
    assert source.count("id:900490") == 1
    # One Mesh rule plus the pre-existing, unrelated /api/tool exclusion.
    assert source.count("ctl:requestBodyAccess=Off") == 2
    mesh_import = (
        'import hivemind_coraza `SecRule REQUEST_URI "@beginsWith /mesh/v1" '
        '"id:900490,phase:1,pass,nolog,ctl:requestBodyAccess=Off"`'
    )
    assert mesh_import in source


def test_disabled_fallback_retains_historical_coraza_body_inspection() -> None:
    source = _source()
    assert "{args[0]}" in source
    assert (
        "import hivemind_coraza `# Mesh feature explicitly disabled: request body "
        "inspection remains enabled.`"
    ) in source


def test_compose_propagates_only_the_strict_mesh_feature_flag_to_waf() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert (
        "HIVEMIND_MESH_ENABLED=${HIVEMIND_MESH_ENABLED:-true}" in compose
    )


@pytest.mark.skipif(
    not os.environ.get("HIVEMIND_TEST_CADDY_BIN"),
    reason="set HIVEMIND_TEST_CADDY_BIN to a Caddy+Coraza+ratelimit binary",
)
@pytest.mark.parametrize("enabled", ["false", "true"])
def test_real_caddy_adapts_both_feature_states(enabled: str) -> None:
    binary = os.environ["HIVEMIND_TEST_CADDY_BIN"]
    completed = subprocess.run(
        [
            binary,
            "adapt",
            "--config",
            os.fspath(CADDYFILE),
            "--adapter",
            "caddyfile",
            "--validate",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "SITE_ADDRESS": ":18080",
            "HIVEMIND_MESH_ENABLED": enabled,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
