# -*- coding: utf-8 -*-
"""Live Memory — Memory Bank as a Service pour agents IA collaboratifs."""

# Hivemind public version (ADR-0018). The starting public SemVer was decided
# by the user at release-cut time (P7-9, 2026-07-07): `1.0.0-beta.1` — a
# SemVer pre-release signalling an unstable public contract per ADR-0018. The
# in-code version tracks the VERSION file so the test
# `tests/test_mcp_server_version.py` keeps both in sync; packaging tools
# normalize the current value to its PEP 440 form in wheel metadata, while
# MCP serverInfo / system_about report the raw string (matching the `v*` git
# tag minus the leading `v`).
__version__ = "1.4.0"
