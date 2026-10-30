"""agentring as an MCP server (stdio): lets any MCP-capable agent message and call the user.

    claude mcp add agentring -- agentring mcp

A small JSON-RPC 2.0 loop over stdin/stdout (newline-delimited), enough for MCP tools; no
SDK, like the voice side. Logs go to stderr: stdout carries only protocol messages.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from typing import Any

from . import __version__
from .config import Settings, home_dir
from .relay_client import RelayClient, RelayError
from .store import Store
from .sync import catch_up

log = logging.getLogger("agentring.mcp")

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "agentring reaches the user on their phone. Use send_message for anything that can wait and get_replies "
    "to read their answers (wait_s to wait for one). Use call only when it is urgent or a conversation is "
    "really needed: the phone rings and a voice AI talks to the user for you, then you get the transcript."
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "send_message",
        "description": "Send a text message to the user's phone (it shows as a notification they can reply to).",
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "The message, max 4000 characters."},
            "reply_to": {"type": "string", "description": "Optional msg_id or call_id this answers."},
        }, "required": ["text"]},
    },
    {
        "name": "get_replies",
        "description": ("The user's new replies (each one is returned once). With wait_s, waits up to that many "
                        "seconds for a reply if there is none yet."),
        "inputSchema": {"type": "object", "properties": {
            "wait_s": {"type": "integer", "minimum": 0, "maximum": 3600,
                       "description": "Seconds to wait for a reply (default 0: return at once)."},
        }},
    },
    {
        "name": "call",
        "description": ("Call the user's phone; it rings like a real call. When they answer, a voice AI tells them "
                        "why you are calling, talks with them, and hangs up when done. Returns the transcript. "
                        "Blocks until the call ends (or nobody answers, ~30 s of ringing; then the reason and context "
                        "are sent as a message, `fallback_msg_id`: read replies with get_replies)."),
        "inputSchema": {"type": "object", "properties": {
            "reason": {"type": "string", "description": "Why you are calling, one sentence; shown on the phone."},
            "context": {"type": "string", "description": ("What the voice AI should know, tell or ask the user "
                                                           "(it knows nothing else about your work).")},
            "urgent": {"type": "boolean", "description": "Ring like an alarm, bypassing Do Not Disturb if allowed."},
        }, "required": ["reason"]},
    },
    {
        "name": "status",
        "description": "Whether the relay is reachable and which phones are paired.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class ToolError(Exception):
    """Reported to the agent as a tool error (isError), not a protocol error."""


class AgentringTools:
    def __init__(self, settings: Settings, *, poll_s: float = 2.0, agent_name: str | None = None,
                 device_ids: list[str] | None = None):
        self.s = settings
        self.agent_name = agent_name  # caller name on the phone; None = the relay's
        self.device_ids = device_ids  # ring only these phones; None = every paired phone
        self.client = RelayClient(settings.relay_url, settings.api_key)
        self.store = Store(settings.db_path)
        self.poll_s = poll_s
        self._call_lock = asyncio.Lock()
        self._db = threading.Lock()  # tool calls run concurrently, each in a thread

    def close(self) -> None:
        self.client.close()
        self.store.close()

    async def send_message(self, text: str, reply_to: str | None = None) -> dict[str, Any]:
        m = await asyncio.to_thread(self.client.post_message, text, reply_to=reply_to)
        with self._db:
            self.store.upsert_message(m["msg_id"], "agent", m["text"], m["created_at"], m.get("reply_to"))
        return {"msg_id": m["msg_id"]}

    def _new_replies(self) -> list[dict[str, Any]]:
        with self._db:
            return self._take_replies()

    def _take_replies(self) -> list[dict[str, Any]]:
        catch_up(self.client, self.store)
        rows = self.store.replies(since=None, undelivered_only=True, limit=100)
        if rows:
            self.store.mark_delivered([r["msg_id"] for r in rows], int(time.time() * 1000))
            for r in rows:
                if r["read_at"] is None:
                    try:
                        self.client.mark_read(r["msg_id"])
                    except RelayError:
                        pass  # the read receipt is cosmetic
        return [{"msg_id": r["msg_id"], "text": r["text"], "reply_to": r["reply_to"],
                 "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"] / 1000))} for r in rows]

    async def get_replies(self, wait_s: int = 0) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, min(int(wait_s), 3600))
        while True:
            replies = await asyncio.to_thread(self._new_replies)
            if replies or time.monotonic() >= deadline:
                return {"replies": replies}
            await asyncio.sleep(self.poll_s)

    async def call(self, reason: str, context: str | None = None, urgent: bool = False) -> dict[str, Any]:
        try:
            from .voice import CallInfo
            from .voice.agents import AgentsConfigError, load_agents
            from .voice.quick import DEFAULT_INSTRUCTIONS, build_agent, missed_call_message
        except ImportError:
            raise ToolError("voice support is not installed: install agentring with the [voice] extra") from None
        from .voice.providers import VoiceConfigError, resolve_voice

        try:  # AGENTRING_VOICE_PROVIDER/_MODEL/_NAME, like `call --ai`
            choice = resolve_voice()
        except VoiceConfigError as e:
            raise ToolError(f"calls: {e}") from None
        agents = None
        if (home_dir() / "agents.yaml").exists():
            try:
                agents = load_agents(home_dir() / "agents.yaml")
            except AgentsConfigError as e:
                raise ToolError(str(e)) from None
        instructions = DEFAULT_INSTRUCTIONS
        if context:
            instructions += f"\n\nWhat the agent that asked for this call wants you to know, tell or ask:\n{context}"
        if self._call_lock.locked():
            raise ToolError("already on a call")
        async with self._call_lock:
            built = await build_agent(choice, instructions=instructions, agents=agents)
            try:
                created = await asyncio.to_thread(self.client.create_call, reason,
                                                  priority="urgent" if urgent else "normal",
                                                  agent_name=self.agent_name, device_ids=self.device_ids)
                r = await built.agent.run(CallInfo(created["call"]["call_id"], reason), created["join"],
                                          relay=self.client)
            finally:
                await built.aclose()  # agents' late answers are stopped; the user got the gist on the call
        if not r.answered:
            try:  # nobody answered: leave the reason (and context) where the user can reply
                r.fallback_msg_id = await asyncio.to_thread(missed_call_message, self.client, r.call_id, reason, context)
            except RelayError as e:
                log.warning("could not send the missed-call message: %s", e)
        out = asdict(r)
        out["transcript"] = [{"who": "user" if role == "user" else "voice_ai", "text": t} for role, t in r.transcript]
        return out

    async def status(self) -> dict[str, Any]:
        st = await asyncio.to_thread(self.client.status)
        devices = await asyncio.to_thread(self.client.list_devices)
        return {"relay": self.s.relay_url, "relay_status": st,
                "phones": [{"device_id": d["device_id"], "name": d["name"]} for d in devices]}


class McpServer:
    """MCP over stdio. `write` gets one JSON message per call (a line on stdout)."""

    def __init__(self, tools: AgentringTools, write: Callable[[str], Awaitable[None] | None]):
        self.tools = tools
        self._write = write
        self._running: dict[Any, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def send(self, msg: dict[str, Any]) -> None:
        async with self._lock:
            r = self._write(json.dumps(msg, ensure_ascii=False))
            if asyncio.iscoroutine(r):
                await r

    async def _reply(self, rid: Any, result: Any = None, error: tuple[int, str] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
        if error:
            msg["error"] = {"code": error[0], "message": error[1]}
        else:
            msg["result"] = result
        await self.send(msg)

    async def handle(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            await self._reply(None, error=(-32700, "parse error"))
            return
        for m in msg if isinstance(msg, list) else [msg]:
            await self._dispatch(m)

    async def _dispatch(self, m: Any) -> None:
        if not isinstance(m, dict) or "method" not in m:
            return  # a response to us (we send no requests) or junk
        method, rid, params = m["method"], m.get("id"), m.get("params") or {}
        if rid is None:  # notification
            if method == "notifications/cancelled":
                t = self._running.get(params.get("requestId"))
                if t:
                    t.cancel()
            return
        if method == "initialize":
            v = params.get("protocolVersion")
            await self._reply(rid, {
                "protocolVersion": v if v in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "agentring", "version": __version__},
                "instructions": INSTRUCTIONS,
            })
        elif method == "ping":
            await self._reply(rid, {})
        elif method == "tools/list":
            await self._reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            # Each call in its own task: a phone call takes minutes and must not block pings or messages.
            t = asyncio.create_task(self._call_tool(rid, params.get("name"), params.get("arguments") or {}))
            self._running[rid] = t
            t.add_done_callback(lambda _t, rid=rid: self._running.pop(rid, None))
        else:
            await self._reply(rid, error=(-32601, f"method not found: {method}"))

    async def _call_tool(self, rid: Any, name: Any, args: dict[str, Any]) -> None:
        names = {t["name"] for t in TOOLS}
        if name not in names:
            await self._reply(rid, error=(-32602, f"unknown tool: {name}"))
            return
        try:
            out = await getattr(self.tools, name)(**args)
            result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                      "structuredContent": out, "isError": False}
        except asyncio.CancelledError:
            return  # the client cancelled it: no response is expected
        except TypeError as e:
            result = _tool_error(f"bad arguments: {e}")
        except RelayError as e:
            result = _tool_error(str(e))
        except ToolError as e:
            result = _tool_error(str(e))
        except Exception as e:
            log.exception("tool %s failed", name)
            result = _tool_error(f"{type(e).__name__}: {e}")
        await self._reply(rid, result)

    async def serve(self, read_line: Callable[[], Awaitable[str]]) -> None:
        try:
            while True:
                line = await read_line()
                if not line:
                    break  # stdin closed: the client is gone
                if line.strip():
                    await self.handle(line)
        finally:
            for t in list(self._running.values()):
                t.cancel()
            await asyncio.gather(*self._running.values(), return_exceptions=True)


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def run_stdio(settings: Settings, *, agent_name: str | None = None, device_ids: list[str] | None = None) -> None:
    async def main() -> None:
        tools = AgentringTools(settings, agent_name=agent_name, device_ids=device_ids)
        out = sys.stdout

        def write(line: str) -> None:
            out.write(line + "\n")
            out.flush()

        try:
            await McpServer(tools, write).serve(lambda: asyncio.to_thread(sys.stdin.readline))
        finally:
            tools.close()

    asyncio.run(main())
