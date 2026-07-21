# -*- coding: utf-8 -*-
"""
MCP Streamable HTTP client for communicating with the MCP server.

This client uses the official MCP SDK (mcp>=1.8.0) with Streamable HTTP
transport. It handles:
- Streamable HTTP connection (single /mcp endpoint)
- MCP handshake (initialize + notifications/initialized) via the SDK
- Tool calls with progress notifications
- Error handling and timeouts

Migration SSE → Streamable HTTP (issue #1):
- Import: mcp.client.sse → mcp.client.streamable_http
- Function: sse_client → streamablehttp_client
- URL: /sse → /mcp
- Context manager: (read, write) → (read, write, _)
"""

import json
import asyncio
import logging
from typing import Optional, Callable

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger("live_mem.cli")


class MCPClient:
    """
    MCP client via Streamable HTTP (official SDK).

    Uses the MCP SDK to handle the full protocol:
    - Streamable HTTP transport (POST/GET /mcp)
    - Automatic handshake (initialize + notifications/initialized)
    - Tool calls with result parsing
    """

    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: float = 300.0,
        call_delay: float = 0.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.call_delay = call_delay  # Delay between calls (seconds)

    @property
    def headers(self) -> dict:
        """HTTP headers with auth."""
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict,
        on_progress: Optional[Callable] = None,
    ) -> dict:
        """
        Calls an MCP tool via Streamable HTTP.

        The MCP SDK automatically handles:
        1. Streamable HTTP connection to /mcp
        2. initialize + notifications/initialized handshake
        3. tools/call invocation
        4. Response parsing

        Args:
            tool_name: Tool name (e.g.: "system_health")
            arguments: Tool parameters
            on_progress: Optional callback for progress notifications

        Returns:
            The tool result (dict)
        """
        mcp_url = f"{self.base_url}/mcp"

        # Delay between calls to avoid TaskGroup errors (sessions too fast)
        if self.call_delay > 0:
            await asyncio.sleep(self.call_delay)

        try:
            async with streamablehttp_client(
                mcp_url,
                headers=self.headers,
                timeout=self.timeout,
                sse_read_timeout=self.timeout,
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    # The SDK handles the initialize handshake automatically
                    await session.initialize()

                    # Call the tool
                    result = await session.call_tool(tool_name, arguments)

                    # Extract text result
                    if result.content and len(result.content) > 0:
                        text = result.content[0].text
                        try:
                            return json.loads(text)
                        except (json.JSONDecodeError, TypeError):
                            return {"status": "ok", "raw": text}

                    return {"status": "ok", "raw": ""}

        except BaseException as e:
            # Unwrap ExceptionGroup / TaskGroup to surface the real error.
            # The MCP SDK uses anyio TaskGroups; on auth failure (401) the
            # actual HTTP error is buried inside an ExceptionGroup.
            cause = e
            while isinstance(cause, BaseExceptionGroup) and cause.exceptions:
                cause = cause.exceptions[0]
            return {
                "status": "error",
                "message": f"MCP error: {cause}",
            }

    async def list_tools(self) -> list:
        """
        Lists available MCP tools on the server.

        Returns:
            List of tools with name and description
        """
        result = await self.call_tool("system_about", {})
        return result.get("tools", [])
