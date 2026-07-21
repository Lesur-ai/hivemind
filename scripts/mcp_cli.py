#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MCP service CLI entry point.

Usage:
    python scripts/mcp_cli.py --help
    python scripts/mcp_cli.py health
    python scripts/mcp_cli.py about
    python scripts/mcp_cli.py shell

Environment variables:
    MCP_URL   — Server URL (default: http://localhost:8002)
    MCP_TOKEN — Authentication token
"""

import sys
from pathlib import Path

# Add parent directory to path for relative imports
sys.path.insert(0, str(Path(__file__).parent))

from cli.commands import cli

if __name__ == "__main__":
    cli()
