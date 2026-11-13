"""Tools the voice model can call during a call: plain Python functions or any MCP server.

A tool is a name, a description, a JSON Schema for its arguments and a handler. The
handler may be sync (run in a thread, so a slow tool never freezes the audio) or async,
and returns anything; non-strings are sent to the model as JSON.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("agentring.voice.tools")

_session: contextvars.ContextVar[Any] = contextvars.ContextVar("agentring_call_session", default=None)


def current_session() -> Any:
    """The CallSession running the current tool (None outside a call). A tool that answers
    later can keep it and call `session.notify(text, respond=True)` when it is done."""
    return _session.get()


@dataclass
class Tool:
    name: str
    description: str
    handler: Callable[..., Any]
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    timeout_s: float | None = None  # None = the agent's default

    async def call(self, args: dict[str, Any]) -> Any:
        if inspect.iscoroutinefunction(self.handler):
            return await self.handler(**args)
        return await asyncio.to_thread(self.handler, **args)


@dataclass
class ToolCall:
    id: str | None
    name: str
    args: dict[str, Any]


def result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


class McpError(Exception):
    pass


class McpClient:
    """Minimal MCP client over Streamable HTTP (JSON or SSE responses): enough for tools."""

    def __init__(self, url: str, *, headers: dict[str, str] | None = None, timeout_s: float = 300):
        self.url = url
        self._http = httpx.AsyncClient(headers={"accept": "application/json, text/event-stream", **(headers or {})},
                                       timeout=timeout_s)
        self._session: str | None = None
        self._next_id = 0

    async def __aenter__(self) -> McpClient:
        await self.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, body: dict[str, Any]) -> dict[str, Any] | None:
        headers = {"mcp-session-id": self._session} if self._session else {}
        r = await self._http.post(self.url, json=body, headers=headers)
        if r.status_code >= 400:
            raise McpError(f"MCP {body.get('method')}: HTTP {r.status_code} {r.text[:200]}")
        self._session = r.headers.get("mcp-session-id", self._session)
        if "id" not in body or r.status_code == 202 or not r.content:
            return None
        if r.headers.get("content-type", "").startswith("text/event-stream"):
            msg = _last_sse_response(r.text, body["id"])
        else:
            msg = r.json()
            if isinstance(msg, list):
                msg = next((m for m in msg if m.get("id") == body["id"]), None)
        if not msg:
            raise McpError(f"MCP {body['method']}: no response")
        if "error" in msg:
            raise McpError(f"MCP {body['method']}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        return await self._post({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}) or {}

    async def initialize(self) -> None:
        await self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "agentring-voice", "version": "1"}})
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def list_tools(self) -> list[dict[str, Any]]:
        tools, cursor = [], None
        while True:
            page = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools += page.get("tools", [])
            cursor = page.get("nextCursor")
            if not cursor:
                return tools

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        res = await self.request("tools/call", {"name": name, "arguments": args})
        text = "\n".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if not text and res.get("structuredContent") is not None:
            text = result_text(res["structuredContent"])
        return f"Error: {text}" if res.get("isError") else text


def _last_sse_response(body: str, rid: Any) -> dict[str, Any] | None:
    found = None
    # SSE lines may end in \r\n or \r as well as \n (sse-starlette, used by the Python MCP SDK, sends \r\n).
    body = body.replace("\r\n", "\n").replace("\r", "\n")
    for block in body.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            try:
                msg = json.loads(data)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == rid:
                found = msg
    return found


async def mcp_tools(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    allow: Iterable[str] | None = None,
    timeout_s: float = 300,
) -> tuple[list[Tool], McpClient]:
    """The tools of an MCP server, ready for a VoiceAgent. Only the `allow` list if given
    (over the phone, expose just what the caller should be able to do).

    Returns the client too: close it (`await client.aclose()`) when you're done.
    """
    client = McpClient(url, headers=headers, timeout_s=timeout_s)
    await client.initialize()
    allowed = set(allow) if allow is not None else None

    def _bind(name: str) -> Callable[..., Any]:
        # The name lives in the closure: every keyword of the handler comes from the model,
        # so a parameter here would let a tool call pick another tool past the allowlist.
        async def handler(**args: Any) -> str:
            return await client.call_tool(name, args)

        return handler

    tools = []
    for t in await client.list_tools():
        if allowed is not None and t["name"] not in allowed:
            continue
        tools.append(Tool(t["name"], t.get("description", ""), _bind(t["name"]),
                          t.get("inputSchema") or {"type": "object", "properties": {}}))
    log.info("MCP %s: %d tools", url, len(tools))
    return tools, client
