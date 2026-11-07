"""Voice AI building blocks, without a phone or a real model."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("livekit")

import websockets  # noqa: E402

from agentring.voice import CallInfo, GeminiLive, Tool, ToolCall, VoiceAgent, is_incoming_call, mcp_tools  # noqa: E402
from agentring.voice.backend import AudioOut, Interrupted, ToolCalls, ToolResult, Transcript, TurnComplete  # noqa: E402
from agentring.voice.gemini import _clean_schema, parse  # noqa: E402
from agentring.voice.session import CallSession  # noqa: E402

# ------------------------------------------------------------------ Gemini protocol


def test_parse_gemini_messages():
    pcm = b"\x01\x00\x02\x00"
    evs = parse({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": base64.b64encode(pcm).decode()}}]},
                                   "outputTranscription": {"text": "Hola"}, "turnComplete": True}})
    assert [type(e) for e in evs] == [AudioOut, Transcript, TurnComplete]
    assert evs[0].pcm == pcm and evs[1].role == "agent" and evs[1].text == "Hola"

    evs = parse({"serverContent": {"interrupted": True, "inputTranscription": {"text": "espera"}}})
    assert [type(e) for e in evs] == [Interrupted, Transcript, TurnComplete]
    assert evs[1].role == "user"

    evs = parse({"toolCall": {"functionCalls": [{"id": "f1", "name": "status", "args": {"x": 1}}, {"id": "f2", "name": "end_call"}]}})
    assert isinstance(evs[0], ToolCalls)
    assert [(c.id, c.name, c.args) for c in evs[0].calls] == [("f1", "status", {"x": 1}), ("f2", "end_call", {})]


def test_schema_cleanup_for_gemini():
    s = _clean_schema({"$schema": "x", "type": "object", "additionalProperties": False, "title": "T",
                       "properties": {"a": {"type": "string", "default": "z", "title": "A"},
                                      "b": {"type": "array", "items": {"type": "integer", "examples": [1]}}},
                       "required": ["a"]})
    assert s == {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "array", "items": {"type": "integer"}}},
                 "required": ["a"]}
    assert _clean_schema({"type": "object", "properties": {}, "required": []}) == {"type": "object", "properties": {}}


async def _fake_gemini(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}"


def test_gemini_backend_wire_protocol():
    seen: list[dict] = []
    headers: dict = {}

    async def handler(ws):
        headers.update(ws.request.headers)
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"setupComplete": {}}))
        async for raw in ws:
            seen.append(json.loads(raw))
            if "clientContent" in seen[-1]:
                await ws.send(json.dumps({"toolCall": {"functionCalls": [{"id": "c1", "name": "ping", "args": {}}]}}))
            if "toolResponse" in seen[-1]:
                await ws.send(json.dumps({"serverContent": {"turnComplete": True}}))
                await ws.close()

    async def run():
        server, url = await _fake_gemini(handler)
        g = GeminiLive("secret-key", model="m-live", voice="Puck", url=url)
        await g.connect("Be brief.", [Tool("ping", "Ping.", lambda: "pong")])
        await g.send_audio(b"\x00\x00" * 4)
        await g.send_text("hello", respond=True)
        got = []
        async for ev in g.events():
            got.append(ev)
            if isinstance(ev, ToolCalls):
                await g.send_tool_results([ToolResult(ev.calls[0], "pong")])
        await g.close()
        server.close()
        return got

    got = asyncio.run(run())
    assert headers["x-goog-api-key"] == "secret-key"
    setup = seen[0]["setup"]
    assert setup["model"] == "models/m-live"
    assert setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Puck"
    assert setup["systemInstruction"]["parts"][0]["text"] == "Be brief."
    assert setup["tools"][0]["functionDeclarations"][0]["name"] == "ping"
    assert seen[1]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert seen[2]["clientContent"]["turnComplete"] is True
    assert seen[3]["toolResponse"]["functionResponses"] == [{"id": "c1", "name": "ping", "response": {"result": "pong"}}]
    assert [type(e) for e in got] == [ToolCalls, TurnComplete]


# ------------------------------------------------------------------ MCP tools


class _Mcp(BaseHTTPRequestHandler):
    sse = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "id" not in msg:
            self.send_response(202)
            self.end_headers()
            return
        m = msg["method"]
        if m == "initialize":
            res = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "t"}}
        elif m == "tools/list":
            res = {"tools": [
                {"name": "status", "description": "Status.", "inputSchema": {"type": "object", "properties": {}}},
                {"name": "deploy", "description": "Deploy.", "inputSchema": {"type": "object", "properties": {"app": {"type": "string"}}}},
                {"name": "broken", "description": "Fails.", "inputSchema": {"type": "object"}}]}
        else:
            name, args = msg["params"]["name"], msg["params"]["arguments"]
            if name == "broken":
                res = {"content": [{"type": "text", "text": "boom"}], "isError": True}
            else:
                res = {"content": [{"type": "text", "text": f"{name} ok {json.dumps(args)}"}]}
        body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res})
        self.send_response(200)
        self.send_header("mcp-session-id", "s1")
        if self.sse:
            data = f"event: message\ndata: {body}\n\n".encode()
            self.send_header("content-type", "text/event-stream")
        else:
            data = body.encode()
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


@pytest.mark.parametrize("sse", [False, True])
def test_mcp_tools(sse):
    handler = type("H", (_Mcp,), {"sse": sse})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/mcp"

    async def run():
        tools, client = await mcp_tools(url, allow=["status", "deploy", "broken"])
        out = {t.name: await t.call({"app": "web"} if t.name == "deploy" else {}) for t in tools}
        only, c2 = await mcp_tools(url, allow=["status"])
        await client.aclose()
        await c2.aclose()
        return out, [t.name for t in only]

    try:
        out, only = asyncio.run(run())
    finally:
        srv.shutdown()
    assert out == {"status": "status ok {}", "deploy": 'deploy ok {"app": "web"}', "broken": "Error: boom"}
    assert only == ["status"]


def test_a_tool_argument_cannot_pick_another_tool():
    """`allow` must hold against the model: `_name` is only an argument of the allowed tool."""
    called: list[str] = []

    class Recording(_Mcp):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            msg = json.loads(body)
            if msg.get("method") == "tools/call":
                called.append(msg["params"]["name"])
            self.rfile = io.BytesIO(body)
            super().do_POST()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Recording)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def run():
        tools, client = await mcp_tools(f"http://127.0.0.1:{srv.server_address[1]}/mcp", allow=["status"])
        try:
            return [t.name for t in tools], await tools[0].call({"_name": "deploy"})
        finally:
            await client.aclose()

    try:
        names, out = asyncio.run(run())
    finally:
        srv.shutdown()
    assert names == ["status"]
    assert called == ["status"]  # never deploy
    assert out == 'status ok {"_name": "deploy"}'


# ------------------------------------------------------------------ the call session


class _NullBackend:
    input_rate, output_rate, max_session_s = 16_000, 24_000, None


def _session(**kw) -> CallSession:
    tools = [
        Tool("add", "Add.", lambda a, b: a + b),
        Tool("slow", "Slow.", lambda: time.sleep(2), timeout_s=0.2),
        Tool("fail", "Fail.", lambda: 1 / 0),
        Tool("adeep", "Async.", _async_tool),
    ]
    agent = VoiceAgent(lambda: _NullBackend(), "x", tools, **kw)
    return CallSession(agent, CallInfo("call_1", "test"))


async def _async_tool(q: str) -> dict:
    return {"answer": q.upper()}


def test_tools_run_and_report_errors():
    async def run():
        s = _session()
        return s, [await s.run_tool(ToolCall("1", "add", {"a": 2, "b": 3})),
                   await s.run_tool(ToolCall("2", "adeep", {"q": "hi"})),
                   await s.run_tool(ToolCall("3", "slow", {})),
                   await s.run_tool(ToolCall("4", "fail", {})),
                   await s.run_tool(ToolCall("5", "nope", {}))]

    s, out = asyncio.run(run())
    assert out[0] == "5"
    assert json.loads(out[1]) == {"answer": "HI"}
    assert out[2].startswith("Still running after 0 s")
    assert out[3].startswith("Error: division by zero")
    assert out[4].startswith("No such tool: nope. Available tools: add")
    assert [t["name"] for t in s.tool_log] == ["add", "adeep", "slow", "fail", "nope"]


def test_unknown_tool_suggests_the_closest_name():
    # A voice model asked for "vulnerabilities" when the tool is "vulnerabilidades" and then gave up.
    async def run():
        return await _session().run_tool(ToolCall("1", "adds", {}))

    assert asyncio.run(run()) == "No such tool: adds. Did you mean add? Call it again with that exact name."


def test_before_tool_can_block_or_rewrite():
    def guard(session, call):
        if call.name == "fail":
            return "Not done: nobody asked for it."
        if call.name == "add":
            return {**call.args, "b": 100}
        return None

    async def run():
        s = _session(before_tool=guard)
        return s, [await s.run_tool(ToolCall("1", "fail", {})), await s.run_tool(ToolCall("2", "add", {"a": 1, "b": 1}))]

    s, out = asyncio.run(run())
    assert out == ["Not done: nobody asked for it.", "101"]
    assert s.tool_log[0]["skipped"] and not s.tool_log[1]["skipped"]
    assert s.tool_log[1]["args"] == {"a": 1, "b": 100}


def test_end_call_tool_and_specs():
    async def run():
        s = _session()
        names = [t.name for t in s.tool_specs()]
        out = await s.run_tool(ToolCall("1", "end_call", {}))
        return s, names, out

    s, names, out = asyncio.run(run())
    assert names[-1] == "end_call" and "add" in names
    assert s._hangup_at > 0 and out.startswith("Hanging up")

    async def without():
        return [t.name for t in _session(end_call_tool=False).tool_specs()]

    assert "end_call" not in asyncio.run(without())


def test_a_backlog_goes_out_in_40_ms_chunks():
    import numpy as np

    async def run():
        s = _session()
        sent: list[int] = []

        async def send_audio(pcm: bytes) -> None:
            sent.append(len(pcm))

        s.backend.send_audio = send_audio
        chunk = 16_000 // 25  # samples per 40 ms
        s.inputs["a"] = bytearray(np.zeros(chunk * 7 + 5, dtype=np.int16).tobytes())  # 280 ms waiting
        await s.send_pending_input()
        return sent, len(s.inputs["a"])

    sent, left = asyncio.run(run())
    assert sent == [1280] * 7 and left == 10


def test_mixing_and_model_events():
    import numpy as np

    async def run():
        s = _session()
        chunk = 16_000 // 25  # samples per 40 ms
        s.inputs["a"] = bytearray(np.full(chunk, 20_000, dtype=np.int16).tobytes())
        s.inputs["b"] = bytearray(np.full(chunk + 10, 20_000, dtype=np.int16).tobytes())
        mixed = np.frombuffer(s.take_mixed_input(), dtype=np.int16)
        rest = (len(s.inputs["a"]), len(s.inputs["b"]))

        s.handle_event(Transcript("user", "hola, "))
        s.handle_event(Transcript("user", "¿qué tal?"))
        s.handle_event(AudioOut(b"\x01\x00" * 100))
        s.handle_event(Transcript("agent", "Bien."))
        s.handle_event(TurnComplete())
        queued = len(s.output)
        s.handle_event(AudioOut(b"\x01\x00" * 100))
        s.handle_event(Interrupted())
        return s, mixed, rest, queued

    s, mixed, rest, queued = asyncio.run(run())
    assert len(mixed) == 640 and mixed.max() == 32767  # summed with clipping
    assert rest == (0, 20)  # the longer one keeps its leftover
    assert s.transcript == [("user", "hola, ¿qué tal?"), ("agent", "Bien.")]
    assert s.last_user_speech_at > 0
    assert queued == 200 and len(s.output) == 0  # interrupted: queued speech dropped


def test_is_incoming_call():
    now = time.time()
    ev = {"type": "call.invite", "direction": "user_to_agent", "call_id": "c", "expires_at": (now + 20) * 1000}
    assert is_incoming_call(ev, now)
    assert not is_incoming_call({**ev, "expires_at": (now - 1) * 1000}, now)
    assert not is_incoming_call({**ev, "direction": "agent_to_user"}, now)
    assert not is_incoming_call({**ev, "type": "call.accept"}, now)


def test_consult_agent_through_the_session():
    import sys

    from agentring.voice import CommandAgent, Consultations

    slow = CommandAgent("ops", "Ops.", [sys.executable, "-c", "import sys, time; time.sleep(0.5); print('ok', sys.argv[1])",
                                        "{task}"])

    async def run():
        c = Consultations([slow], answer_within_s=0.1)
        agent = VoiceAgent(lambda: _NullBackend(), "x", [c.tool()])
        s = CallSession(agent, CallInfo("call_1", "test"))
        out = await s.run_tool(ToolCall("1", "consult_agent", {"agent": "ops", "task": "disk"}))
        await c.drain(5)
        return out, s._notes.get_nowait()

    out, (note, respond, trusted) = asyncio.run(run())
    assert "working on it" in out
    assert "ok disk" in note and respond is True
    assert trusted is False  # the agent's output is not the session's own note


def test_notes_loop_passes_provenance_only_to_backends_that_take_it():
    class Old(_NullBackend):
        def __init__(self):
            self.sent = []

        async def send_text(self, text, *, respond):
            self.sent.append((text, respond))

    class New(Old):
        async def send_text(self, text, *, respond, trusted=True):
            self.sent.append((text, respond, trusted))

    async def run(backend):
        s = CallSession(VoiceAgent(lambda: backend, "x"), CallInfo("call_1", "test"))
        loop = asyncio.create_task(s._notes_loop())
        s.notify("mine")
        s.notify("theirs", respond=True, trusted=False)
        await asyncio.sleep(0.05)
        loop.cancel()
        return backend.sent

    assert asyncio.run(run(Old())) == [("mine", False), ("theirs", True)]
    assert asyncio.run(run(New())) == [("mine", False, True), ("theirs", True, False)]


def test_after_user_speech_gate_holds_tools_until_the_user_speaks():
    from agentring.voice.quick import NO_USER_YET, after_user_speech

    async def run():
        ran: list[int] = []
        tool = Tool("ping", "Ping.", lambda: ran.append(1) or "pong")
        s = CallSession(VoiceAgent(lambda: _NullBackend(), "x", [tool], before_tool=after_user_speech),
                        CallInfo("call_1", "test"))
        before = await s.run_tool(ToolCall("1", "ping", {}))
        hangs_up = await s.run_tool(ToolCall("2", "end_call", {}))  # hanging up is never held back
        s.handle_event(Transcript("user", "   "))  # silence is not speech
        still = await s.run_tool(ToolCall("3", "ping", {}))
        s.handle_event(Transcript("user", "go ahead"))
        after = await s.run_tool(ToolCall("4", "ping", {}))
        return before, hangs_up, still, after, ran, s.tool_log

    before, hangs_up, still, after, ran, log = asyncio.run(run())
    assert before == still == NO_USER_YET and "Hanging up" in hangs_up
    assert after == "pong" and ran == [1]
    assert [t["skipped"] for t in log] == [True, False, True, False]


class _FakeBuilt:
    """What `build_agent` gives `AgentringTools.call`, for a call nobody answers."""

    consultations = None

    def __init__(self, seen: dict):
        self.seen = seen
        self.agent = self

    async def run(self, info, join, *, relay=None):
        from agentring.voice.session import CallResult

        self.seen["info"] = info
        return CallResult(info.call_id, False, 0, "no_answer")

    async def aclose(self):
        pass


class _FakeClient:
    relay_url = "http://127.0.0.1:1"

    def __init__(self):
        self.messages: list[tuple[str, str | None]] = []
        self.create_args: dict = {}

    def create_call(self, reason, **kw):
        self.create_args = kw
        return {"call": {"call_id": "call_1"}, "join": {}}

    def post_message(self, text, *, reply_to=None):
        self.messages.append((text, reply_to))
        return {"msg_id": "msg_1"}


def _mcp_call(monkeypatch, tmp_path, **kw):
    """Runs the MCP `call` tool with the voice stack stubbed; returns (result, client, what it was given)."""
    from agentring.mcp_server import AgentringTools
    from agentring.store import Store
    from agentring.voice import providers, quick

    seen: dict = {}

    async def fake_build(choice, **build_kw):
        seen.update(build_kw)
        return _FakeBuilt(seen)

    monkeypatch.setattr(quick, "build_agent", fake_build)
    monkeypatch.setattr(providers, "resolve_voice", lambda: object())
    client = _FakeClient()
    tools = AgentringTools(client, Store(tmp_path / "s.db"), agents_file=None, **kw)
    out = asyncio.run(tools.call("Deploy blocked", context="Need OK for the migration."))
    tools.store.close()
    return out, client, seen


def test_missed_targeted_call_sends_no_message(monkeypatch, tmp_path):
    out, client, seen = _mcp_call(monkeypatch, tmp_path, device_ids=["dev_A"])
    assert client.create_args["device_ids"] == ["dev_A"] and seen["info"].device_ids == ["dev_A"]
    assert client.messages == [] and out["fallback_msg_id"] is None


def test_missed_group_call_still_leaves_a_message(monkeypatch, tmp_path):
    out, client, seen = _mcp_call(monkeypatch, tmp_path)
    assert seen["info"].device_ids is None
    assert client.messages == [("Missed call: Deploy blocked\n\nNeed OK for the migration.", "call_1")]
    assert out["fallback_msg_id"] == "msg_1"


def test_mcp_call_gates_tools_by_default(monkeypatch, tmp_path):
    from agentring.voice.quick import after_user_speech

    assert _mcp_call(monkeypatch, tmp_path)[2]["before_tool"] is after_user_speech

    def mine(session, call):
        return None

    assert _mcp_call(monkeypatch, tmp_path, before_tool=mine)[2]["before_tool"] is mine


def test_build_agent_passes_before_tool(monkeypatch):
    from agentring.voice import quick

    def gate(session, call):
        return None

    choice = type("Choice", (), {"factory": lambda self: _NullBackend})()
    built = asyncio.run(quick.build_agent(choice, before_tool=gate))
    assert built.agent.before_tool is gate
    assert asyncio.run(quick.build_agent(choice)).agent.before_tool is None  # `call --ai`, `serve --ai`: unchanged


def test_instructions_ask_to_talk_before_tools():
    from agentring.voice import Prompts

    async def run():
        text = await _session()._instructions()
        assert text.startswith("x\n\n") and "let me check" in text and "Never answer from memory" in text
        agent = VoiceAgent(lambda: _NullBackend(), "x", [])  # no tools: nothing to add
        assert await CallSession(agent, CallInfo("c", "t"))._instructions() == "x"
        agent = VoiceAgent(lambda: _NullBackend(), "x", [Tool("add", "Add.", lambda a, b: a + b)],
                           prompts=Prompts(before_tools=""))
        assert await CallSession(agent, CallInfo("c", "t"))._instructions() == "x"

    asyncio.run(run())
