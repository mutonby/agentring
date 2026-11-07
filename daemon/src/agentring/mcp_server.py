"""agentring as an MCP server: lets any MCP-capable agent message and call the user.

    claude mcp add agentring -- agentring mcp                                  # stdio
    claude mcp add --transport http agentring https://host/mcp \\
        --header "Authorization: Bearer ak_..."                                 # mcp_http.py

This module holds what both transports share, the tools (`AgentringTools`) and the
JSON-RPC handling (`handle_rpc`), and the stdio transport: a small JSON-RPC 2.0 loop over
stdin/stdout (newline-delimited). No SDK, like the voice side. Logs go to stderr: stdout
carries only protocol messages.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
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
                        "are sent as a message, `fallback_msg_id`: read replies with get_replies; not when this "
                        "server rings only some phones: then nothing is sent and `fallback_msg_id` is null)."),
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


class _Busy:
    """A thread-safe counter used as a context manager around worker-thread work."""

    def __init__(self) -> None:
        self.n = 0
        self._lock = threading.Lock()

    def __enter__(self) -> _Busy:
        with self._lock:
            self.n += 1
        return self

    def __exit__(self, *exc: object) -> None:
        with self._lock:
            self.n -= 1


class CallSlots:
    """A cap on concurrent voice calls, shared by every user of one server (each call holds a
    voice AI session and its audio). Used from the event loop only."""

    def __init__(self, limit: int):
        self.limit = max(0, limit)
        self.used = 0

    def try_acquire(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True

    def release(self) -> None:
        self.used = max(0, self.used - 1)


def quota_note(upgrade_url: str | None) -> str:
    """What the agent reads when the user's hosted plan ran out (`quota_exceeded`, or a call
    the relay ended because the month's minutes were used up)."""
    note = "The user's agentring plan limit is reached."
    if upgrade_url:
        note += f" They can upgrade to Pro at {upgrade_url} ; tell them, with this link."
    return note


class AgentringTools:
    """The MCP tools for one relay (one user). Transport-agnostic: the stdio server has one,
    the HTTP server one per tenant.

    `store` mirrors the relay's messages and remembers which replies get_replies already
    returned. `agents_file` (agents.yaml) gives the voice AI the user's agents and MCP
    servers on calls; None = none (the HTTP server never runs local commands for its users).
    `call_slots` caps voice calls across instances. `before_tool` gates the voice AI's tools on
    calls (see `VoiceAgent`); the default is `after_user_speech`, because `context` comes from the
    calling agent, which may be passing on text it read elsewhere: nothing runs on that text alone.
    `upgrade_url` (hosted relays) is where the user can raise their plan's limits; it goes into
    quota errors."""

    def __init__(self, client: RelayClient, store: Store, *, relay_url: str | None = None, poll_s: float = 2.0,
                 agent_name: str | None = None, device_ids: list[str] | None = None,
                 agents_file: Path | None = None, call_slots: CallSlots | None = None, before_tool: Any = None,
                 upgrade_url: str | None = None):
        self.client = client
        self.store = store
        self.relay_url = relay_url or client.relay_url
        self.agent_name = agent_name  # caller name on the phone; None = the relay's
        self.device_ids = device_ids  # ring only these phones; None = every paired phone
        self.agents_file = agents_file
        self.call_slots = call_slots
        self.before_tool = before_tool
        self.upgrade_url = upgrade_url
        self.poll_s = poll_s
        self._call_lock = asyncio.Lock()
        self._db = threading.Lock()  # tool calls run concurrently, each in a thread
        self._busy = _Busy()  # worker threads still running (also ones whose request was cancelled)

    @classmethod
    def from_settings(cls, settings: Settings, **kw: Any) -> AgentringTools:
        """The local daemon's relay, store and agents.yaml (what `agentring mcp` uses)."""
        kw.setdefault("agents_file", home_dir() / "agents.yaml")
        kw.setdefault("upgrade_url", os.environ.get("AGENTRING_UPGRADE_URL") or None)
        return cls(RelayClient(settings.relay_url, settings.api_key), Store(settings.db_path),
                   relay_url=settings.relay_url, **kw)

    def close(self) -> None:
        self.client.close()
        self.store.close()

    async def send_message(self, text: str, reply_to: str | None = None) -> dict[str, Any]:
        m = await asyncio.to_thread(self.client.post_message, text, reply_to=reply_to)
        await asyncio.to_thread(self._record_sent, m)  # not on the event loop: get_replies may hold the lock
        return {"msg_id": m["msg_id"]}

    def _record_sent(self, m: dict[str, Any]) -> None:
        with self._db:
            self.store.upsert_message(m["msg_id"], "agent", m["text"], m["created_at"], m.get("reply_to"))

    @property
    def busy(self) -> bool:
        """A worker thread still uses the store or a client (see `with_client`)."""
        return self._busy.n > 0

    def with_client(self, client: RelayClient, relay_url: str | None = None) -> AgentringTools:
        """The same tools (store, locks, call slot) with another relay client: the HTTP server
        gives each request the client of the key it came with."""
        view = copy.copy(self)
        view.client = client
        view.relay_url = relay_url or client.relay_url
        return view

    def _new_replies(self, cancelled: threading.Event | None = None) -> list[dict[str, Any]]:
        with self._busy:
            return self._take_replies(cancelled)

    def _take_replies(self, cancelled: threading.Event | None = None) -> list[dict[str, Any]]:
        # _db guards the store only. Held across a relay request, it would park this tenant's
        # waiting threads in the pool the HTTP server shares with every other tenant.
        catch_up(self.client, self.store, lock=self._db)
        with self._db:  # claiming replies is one step: each is returned once
            rows = self.store.replies(since=None, undelivered_only=True, limit=100)
            if cancelled is not None and cancelled.is_set():
                return []  # nobody will get them: leave them for the next get_replies
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
            cancelled = threading.Event()
            try:
                replies = await asyncio.to_thread(self._new_replies, cancelled)
            except asyncio.CancelledError:
                cancelled.set()  # the thread goes on, but won't mark anything as returned
                raise
            if replies or time.monotonic() >= deadline:
                return {"replies": replies}
            await asyncio.sleep(self.poll_s)

    async def call(self, reason: str, context: str | None = None, urgent: bool = False) -> dict[str, Any]:
        try:
            from .voice import CallInfo
            from .voice.agents import AgentsConfigError, load_agents
            from .voice.quick import DEFAULT_INSTRUCTIONS, after_user_speech, build_agent, missed_call_message
        except ImportError:
            raise ToolError("voice support is not installed: install agentring with the [voice] extra") from None
        from .voice.providers import VoiceConfigError, resolve_voice

        try:  # AGENTRING_VOICE_PROVIDER/_MODEL/_NAME, like `call --ai`
            choice = resolve_voice()
        except VoiceConfigError as e:
            raise ToolError(f"calls: {e}") from None
        agents = None
        if self.agents_file is not None and self.agents_file.exists():
            try:
                agents = load_agents(self.agents_file)
            except AgentsConfigError as e:
                raise ToolError(str(e)) from None
        instructions = DEFAULT_INSTRUCTIONS
        if context:
            instructions += f"\n\nWhat the agent that asked for this call wants you to know, tell or ask:\n{context}"
        if self._call_lock.locked():
            raise ToolError("already on a call")
        if self.call_slots is not None and not self.call_slots.try_acquire():
            raise ToolError("busy: this server is on as many calls as it allows; try again in a few minutes, "
                            "or use send_message")
        try:
            async with self._call_lock:
                built = await build_agent(choice, instructions=instructions, agents=agents,
                                          before_tool=self.before_tool or after_user_speech)
                try:
                    created = await asyncio.to_thread(self.client.create_call, reason,
                                                      priority="urgent" if urgent else "normal",
                                                      agent_name=self.agent_name, device_ids=self.device_ids)
                    r = await built.agent.run(CallInfo(created["call"]["call_id"], reason, device_ids=self.device_ids),
                                              created["join"], relay=self.client)
                finally:
                    await built.aclose()  # agents' late answers are stopped; the user got the gist on the call
        finally:
            if self.call_slots is not None:
                self.call_slots.release()
        # A targeted call is private to its phones, and a message reaches every paired phone.
        if not r.answered and self.device_ids is None:
            try:  # nobody answered: leave the reason (and context) where the user can reply
                r.fallback_msg_id = await asyncio.to_thread(missed_call_message, self.client, r.call_id, reason, context)
            except RelayError as e:
                log.warning("could not send the missed-call message: %s", e)
        out = asdict(r)
        if r.answered and r.end_reason != "end_call":
            note = await self.quota_cutoff_note(r.call_id)
            if note:
                out["note"] = note
        out["transcript"] = [{"who": "user" if role == "user" else "voice_ai", "text": t} for role, t in r.transcript]
        return out

    async def quota_cutoff_note(self, call_id: str) -> str | None:
        """A note for the agent when the relay ended the call because the month's minutes ran out."""
        try:
            call = await asyncio.to_thread(self.client.get_call, call_id)
        except RelayError:
            return None
        if call.get("end_reason") != "quota":
            return None
        return "The call was cut off: the month's call minutes ran out. " + quota_note(self.upgrade_url)

    async def status(self) -> dict[str, Any]:
        st = await asyncio.to_thread(self.client.status)
        devices = await asyncio.to_thread(self.client.list_devices)
        return {"relay": self.relay_url, "relay_status": st,
                "phones": [{"device_id": d["device_id"], "name": d["name"]} for d in devices]}



# ------------------------------------------------------------------ JSON-RPC (both transports)

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)


def valid_id(rid: Any) -> bool:
    """JSON-RPC ids are strings or numbers (null is only for errors)."""
    return isinstance(rid, str) or (isinstance(rid, (int, float)) and not isinstance(rid, bool))


def is_request(m: Any) -> bool:
    """A JSON-RPC request (needs a response); notifications and responses are not."""
    return isinstance(m, dict) and "method" in m and m.get("id") is not None


def bad_request_id(m: Any) -> bool:
    """A request whose id is not a string or number: answered with -32600 and id null."""
    return is_request(m) and not valid_id(m["id"])


def cancelled_request_id(m: Any) -> Any:
    """The requestId of a notifications/cancelled, if it is a usable id."""
    if isinstance(m, dict) and m.get("method") == "notifications/cancelled":
        params = m.get("params")
        rid = params.get("requestId") if isinstance(params, dict) else None
        return rid if valid_id(rid) else None
    return None


def rpc_response(rid: Any, result: Any = None, error: tuple[int, str] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid}
    if error:
        msg["error"] = {"code": error[0], "message": error[1]}
    else:
        msg["result"] = result
    return msg


def negotiate_version(requested: Any) -> str:
    return requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]


def initialize_result(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": negotiate_version(params.get("protocolVersion")),
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": "agentring", "version": __version__},
        "instructions": INSTRUCTIONS,
    }


async def call_tool(tools: AgentringTools, name: Any, args: Any) -> dict[str, Any]:
    """Runs one tool; any failure is a tool result with isError (the agent reads it). Cancellation
    propagates: a cancelled request gets no response."""
    try:
        if not isinstance(args, dict):
            raise TypeError("arguments must be an object")
        out = await getattr(tools, name)(**args)
        return {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                "structuredContent": out, "isError": False}
    except TypeError as e:
        return _tool_error(f"bad arguments: {e}")
    except RelayError as e:
        text = str(e)
        if e.code == "quota_exceeded":
            text += " " + quota_note(tools.upgrade_url)
        return _tool_error(text)
    except ToolError as e:
        return _tool_error(str(e))
    except Exception as e:
        log.exception("tool %s failed", name)
        return _tool_error(f"{type(e).__name__}: {e}")


async def handle_rpc(tools: AgentringTools, m: Any) -> dict[str, Any] | None:
    """One JSON-RPC message to its response; None for notifications and responses (transports
    handle `notifications/cancelled` themselves). `tools/call` waits for the tool, which may take
    minutes (`call`)."""
    if not is_request(m):
        return None
    method, rid, params = m["method"], m["id"], m.get("params") or {}
    if not isinstance(params, dict):
        return rpc_response(rid, error=(-32602, "params must be an object"))
    if method == "initialize":
        return rpc_response(rid, initialize_result(params))
    if method == "ping":
        return rpc_response(rid, {})
    if method == "tools/list":
        return rpc_response(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOL_NAMES:
            return rpc_response(rid, error=(-32602, f"unknown tool: {name}"))
        return rpc_response(rid, await call_tool(tools, name, params.get("arguments") or {}))
    return rpc_response(rid, error=(-32601, f"method not found: {method}"))


def _tool_error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": True}


# ------------------------------------------------------------------ stdio transport


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

    async def handle(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except ValueError:
            await self.send(rpc_response(None, error=(-32700, "parse error")))
            return
        for m in msg if isinstance(msg, list) else [msg]:
            await self._dispatch(m)

    async def _dispatch(self, m: Any) -> None:
        if bad_request_id(m):
            await self.send(rpc_response(None, error=(-32600, "invalid request: id must be a string or a number")))
            return
        if not is_request(m):
            rid = cancelled_request_id(m)
            t = self._running.get(rid) if rid is not None else None
            if t:
                t.cancel()
            return  # other notifications, a response to us (we send no requests) or junk
        if m["method"] == "tools/call":
            # Each call in its own task: a phone call takes minutes and must not block pings or messages.
            rid = m["id"]
            t = asyncio.create_task(self._respond(m))
            self._running[rid] = t
            t.add_done_callback(lambda _t, rid=rid: self._running.pop(rid, None))
        else:
            await self._respond(m)

    async def _respond(self, m: dict[str, Any]) -> None:
        try:
            resp = await handle_rpc(self.tools, m)
        except asyncio.CancelledError:
            return  # the client cancelled it: no response is expected
        if resp is not None:
            await self.send(resp)

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


def run_stdio(settings: Settings, *, agent_name: str | None = None, device_ids: list[str] | None = None) -> None:
    async def main() -> None:
        tools = AgentringTools.from_settings(settings, agent_name=agent_name, device_ids=device_ids)
        out = sys.stdout

        def write(line: str) -> None:
            out.write(line + "\n")
            out.flush()

        try:
            await McpServer(tools, write).serve(lambda: asyncio.to_thread(sys.stdin.readline))
        finally:
            tools.close()

    asyncio.run(main())
