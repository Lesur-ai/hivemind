# -*- coding: utf-8 -*-
"""
Regression tests for MCP initialize/serverInfo version reporting.

FastMCP defaults the low-level server version to the installed `mcp` package
version when the application does not set it explicitly. Live Memory must expose
its own VERSION value instead.
"""

from importlib.metadata import version as package_version
from pathlib import Path

import live_mem
from live_mem import server


def test_mcp_server_info_version_uses_live_memory_version():
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    expected = version_file.read_text(encoding="utf-8").strip()
    sdk_version = package_version("mcp")

    actual = server.mcp._mcp_server.version

    assert actual == expected
    assert actual == live_mem.__version__
    assert actual != sdk_version, (
        "MCP serverInfo.version fell back to the SDK package version instead "
        "of Live Memory's application version."
    )
