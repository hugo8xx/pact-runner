"""How the Runner talks to the board: the same MCP tools every agent uses, over Streamable HTTP
with the runner's bearer token. ``BoardClient`` is the seam tests replace."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

FATAL = frozenset({"system_halted", "agent_paused", "agent_unknown", "mandate_revoked", "mandate_expired", "chain_broken"})
"""Refusals that mean this runner must stop, not retry: the kill switch, a paused agent, a dead mandate."""


class BoardRefusal(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class BoardClient(Protocol):
    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


class McpBoard:
    """One MCP session per call: the Runner calls rarely, and a fresh session never goes stale
    across a sleeping laptop or a board redeploy."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
            async with streamable_http_client(self.url, http_client=http, terminate_on_close=False) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    yield s

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        async with self._session() as s:
            result = await s.call_tool(tool, {k: v for k, v in args.items() if v is not None})
        text = "".join(getattr(c, "text", "") for c in result.content)
        try:
            body = json.loads(text) if text else {}
        except ValueError:
            body = {"error": "invalid_response", "message": text[:500]}
        if result.is_error:
            raise BoardRefusal(str(body.get("error", "error")), str(body.get("message", "")))
        return dict(body)
