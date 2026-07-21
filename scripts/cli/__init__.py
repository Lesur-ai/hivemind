# -*- coding: utf-8 -*-
"""
Global CLI configuration.

Environment variables:
    MCP_URL   — MCP server URL (default: http://localhost:8080 via WAF)
    MCP_TOKEN — Authentication token (or ADMIN_BOOTSTRAP_KEY)

Token resolution priority:
    1. --token parameter
    2. MCP_TOKEN variable
    3. ADMIN_BOOTSTRAP_KEY variable
    4. Read from .env (ADMIN_BOOTSTRAP_KEY=...)
"""

import os
from pathlib import Path

BASE_URL = os.environ.get("MCP_URL", "http://localhost:8080")


def _resolve_token() -> str:
    """Resolves the token by priority order."""
    # 1. MCP_TOKEN variable
    token = os.environ.get("MCP_TOKEN", "")
    if token:
        return token

    # 2. ADMIN_BOOTSTRAP_KEY variable
    token = os.environ.get("ADMIN_BOOTSTRAP_KEY", "")
    if token:
        return token

    # 3. Read from .env
    env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("ADMIN_BOOTSTRAP_KEY=") and not line.startswith("#"):
                return line.split("=", 1)[1].strip()

    return ""


TOKEN = _resolve_token()
