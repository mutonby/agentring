"""OpenAI Realtime, Grok Voice and Phonic backends (wire protocol against fake servers) and provider choice."""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
import websockets

from agentring.voice.backend import AudioOut, Interrupted, ToolCalls, ToolResult, Transcript, TurnComplete
from agentring.voice.openai_realtime import GrokVoice, OpenAIRealtime
from agentring.voice.phonic import Phonic, strict_schema
from agentring.voice.providers import VoiceConfigError, resolve_voice
from agentring.voice.tools import Tool

PCM = b"\x01\x00\x02\x00"
B64 = base64.b64encode(PCM).decode()


async def _serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    return server, f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"


# ------------------------------------------------------------------ OpenAI Realtime


def test_parse_openai_events():
    o = OpenAIRealtime("k")
    assert [type(e) for e in o.parse({"type": "response.output_audio.delta", "delta": B64})] == [AudioOut]
    assert o.parse({"type": "response.output_audio.delta", "delta": B64})[0].pcm == PCM
    ev = o.parse({"type": "response.output_audio_transcript.delta", "delta": "Hola"})[0]
    assert isinstance(ev, Transcript) and (ev.role, ev.text) == ("agent", "Hola")
    # Grok resends the whole text so far: the latest per item counts, handed over at the turn's end.
    for text in ("Vale.", "Vale, perfecto. Gra...", "Vale, perfecto. Gracias."):
        assert o.parse({"type": "conversation.item.input_audio_transcription.completed", "item_id": "i1",
                        "transcript": text}) == []
    o.parse({"type": "conversation.item.input_audio_transcription.completed", "item_id": "i2", "transcript": "Adiós."})
    evs = o.parse({"type": "response.done", "response": {"output": []}})
    assert [type(e) for e in evs] == [Transcript, TurnComplete]
    assert (evs[0].role, evs[0].text) == ("user", "Vale, perfecto. Gracias. Adiós.")
    assert [type(e) for e in o.parse({"type": "input_audio_buffer.speech_started"})] == [Interrupted]
    assert o.parse({"type": "error", "error": {"message": "nope"}}) == []  # logged, not fatal

    # Two calls in one response come out together at response.done, once each.
    assert o.parse({"type": "response.function_call_arguments.done", "call_id": "c1", "name": "status",
                    "arguments": '{"service": "api"}'}) == []
    evs = o.parse({"type": "response.done", "response": {"output": [
        {"type": "function_call", "call_id": "c1", "name": "status", "arguments": '{"service": "api"}'},
        {"type": "function_call", "call_id": "c2", "name": "end_call", "arguments": "not json"},
        {"type": "message"}]}})
    assert [type(e) for e in evs] == [ToolCalls, TurnComplete]
    assert [(c.id, c.name, c.args) for c in evs[0].calls] == [("c1", "status", {"service": "api"}), ("c2", "end_call", {})]
    assert [type(e) for e in o.parse({"type": "response.done", "response": {"output": []}})] == [TurnComplete]


def test_openai_backend_wire_protocol():
    seen: list[dict] = []
    headers: dict = {}
    paths: list[str] = []

    async def handler(ws):
        headers.update(ws.request.headers)
        paths.append(ws.request.path)
        await ws.send(json.dumps({"type": "session.created"}))
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "session.updated"}))
        async for raw in ws:
            seen.append(json.loads(raw))
            t = seen[-1]["type"]
            if t == "response.create" and len([m for m in seen if m["type"] == "response.create"]) == 1:
                await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": B64}))
                await ws.send(json.dumps({"type": "response.function_call_arguments.done", "call_id": "c1",
                                          "name": "ping", "arguments": "{}"}))
                await ws.send(json.dumps({"type": "response.done", "response": {"output": []}}))
            elif t == "response.create":
                await ws.close()

    async def run():
        server, url = await _serve(handler)
        o = OpenAIRealtime("secret-key", model="gpt-x", voice="cedar", url=url)
        await o.connect("Be brief.", [Tool("ping", "Ping.", lambda: "pong",
                                           {"$schema": "x", "type": "object", "properties": {}})])
        await o.send_audio(b"\x00\x00" * 4)
        await o.send_text("note", respond=False)
        await o.send_text("hello", respond=True)
        got = []
        async for ev in o.events():
            got.append(ev)
            if isinstance(ev, ToolCalls):
                await o.send_tool_results([ToolResult(ev.calls[0], "pong")])
        await o.close()
        server.close()
        return got

    got = asyncio.run(run())
    assert headers["authorization"] == "Bearer secret-key"
    assert paths == ["/?model=gpt-x"]
    s = seen[0]["session"]
    assert seen[0]["type"] == "session.update" and s["type"] == "realtime" and s["model"] == "gpt-x"
    assert s["instructions"] == "Be brief." and s["output_modalities"] == ["audio"]
    assert s["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
    assert s["audio"]["input"]["turn_detection"] == {"type": "server_vad"}
    assert s["audio"]["input"]["transcription"]["model"]
    assert s["audio"]["output"]["voice"] == "cedar"
    assert s["tools"] == [{"type": "function", "name": "ping", "description": "Ping.",
                           "parameters": {"type": "object", "properties": {}}}]
    types = [m["type"] for m in seen[1:]]
    assert types == ["input_audio_buffer.append", "conversation.item.create", "conversation.item.create",
                     "response.create", "conversation.item.create", "response.create"]
    assert seen[2]["item"]["content"] == [{"type": "input_text", "text": "note"}]
    assert seen[5]["item"] == {"type": "function_call_output", "call_id": "c1", "output": "pong"}
    assert [type(e) for e in got] == [AudioOut, ToolCalls, TurnComplete]


def test_openai_setup_error_is_raised():
    async def handler(ws):
        await ws.recv()
        await ws.send(json.dumps({"type": "error", "error": {"message": "bad voice"}}))

    async def run():
        server, url = await _serve(handler)
        try:
            with pytest.raises(RuntimeError, match="bad voice"):
                await OpenAIRealtime("k", url=url).connect("x", [])
        finally:
            server.close()

    asyncio.run(run())


def test_grok_session_is_flatter():
    g = GrokVoice("k", voice="eve", language="es-ES")
    s = g.session_config("Be brief.", [Tool("ping", "Ping.", lambda: "pong")])
    assert "type" not in s and "model" not in s and "tool_choice" not in s
    assert s["voice"] == "eve" and s["turn_detection"] == {"type": "server_vad"}
    assert s["audio"]["input"] == {"format": {"type": "audio/pcm", "rate": 24000},
                                   "transcription": {"model": "grok-transcribe", "language_hint": "es-ES"}}
    assert OpenAIRealtime("k", language="es-ES").session_config("x", [])["audio"]["input"]["transcription"]["language"] == "es"
    assert s["tools"][0]["name"] == "ping"
    assert "voice" not in GrokVoice("k").session_config("x", [])  # xAI's default voice
    assert g.url == "wss://api.x.ai/v1/realtime" and g.model == "grok-voice-latest"


# ------------------------------------------------------------------ Phonic


def test_strict_schema():
    s = strict_schema({"$schema": "x", "type": "object", "title": "T", "required": ["title"], "properties": {
        "title": {"type": "string", "default": "z"},
        "labels": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}}}},
        "n": {"type": ["integer", "string"]}}})
    assert s == {"type": "object", "additionalProperties": False, "required": ["title", "labels", "n"], "properties": {
        "title": {"type": "string"},
        "labels": {"type": ["array", "null"], "items": {"type": "object", "additionalProperties": False,
                                                        "required": ["name"], "properties": {"name": {"type": ["string", "null"]}}}},
        "n": {"type": ["integer", "string", "null"]}}}
    assert strict_schema({"type": "object", "properties": {}}) == {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False}


def test_parse_phonic_events():
    p = Phonic("k")
    p.config("x", [Tool("issue", "Open an issue.", lambda **kw: kw, {"type": "object", "required": ["title"],
                                                                      "properties": {"title": {"type": "string"},
                                                                                     "labels": {"type": "array"}}})])
    # Silence between turns is not the model talking.
    assert p.parse({"type": "audio_chunk", "audio": B64, "text": ""}) == []
    assert p.parse({"type": "assistant_started_speaking"}) == []
    evs = p.parse({"type": "audio_chunk", "audio": B64, "text": "Hola "})
    assert [type(e) for e in evs] == [AudioOut, Transcript] and evs[0].pcm == PCM and evs[1].text == "Hola "
    assert [type(e) for e in p.parse({"type": "interrupted_response", "text": "Ho"})] == [Interrupted, TurnComplete]
    assert p.parse({"type": "audio_chunk", "audio": B64}) == []
    ev = p.parse({"type": "input_text", "language": "es", "text": "espera"})[0]
    assert (ev.role, ev.text) == ("user", "espera ")  # word by word: the session joins the pieces
    p.parse({"type": "assistant_started_speaking"})
    assert [type(e) for e in p.parse({"type": "assistant_finished_speaking"})] == [TurnComplete]
    assert p.parse({"type": "audio_chunk", "audio": B64}) == []
    # Optional arguments the model left null are dropped; required ones are kept as they are.
    ev = p.parse({"type": "tool_call", "tool_call_id": "t1", "tool_name": "issue",
                  "parameters": {"title": None, "labels": None}})[0]
    assert isinstance(ev, ToolCalls) and (ev.calls[0].id, ev.calls[0].args) == ("t1", {"title": None})


def test_phonic_backend_wire_protocol():
    seen: list[dict] = []
    headers: dict = {}

    async def handler(ws):
        headers.update(ws.request.headers)
        seen.append(json.loads(await ws.recv()))
        await ws.send(json.dumps({"type": "conversation_created", "conversation_id": "c"}))
        await ws.send(json.dumps({"type": "ready_to_start_conversation"}))
        async for raw in ws:
            seen.append(json.loads(raw))
            t = seen[-1]["type"]
            if t == "generate_reply":
                await ws.send(json.dumps({"type": "tool_call", "tool_call_id": "t1", "tool_name": "ping", "parameters": {}}))
            elif t == "tool_call_output":
                for m in ({"type": "assistant_started_speaking"}, {"type": "audio_chunk", "audio": B64, "text": "pong"},
                          {"type": "assistant_finished_speaking"}):
                    await ws.send(json.dumps(m))
                await ws.close()

    async def run():
        server, url = await _serve(handler)
        p = Phonic("secret-key", voice="sabrina", url=url, language="es-ES")
        await p.connect("Be brief.", [Tool("ping", "Ping.", lambda: "pong"), Tool("end_call", "Hang up.", lambda: None)])
        await p.send_audio(b"\x00\x00" * 4)
        await p.send_text("note", respond=False)
        await p.send_text("greet", respond=True)
        got = []
        async for ev in p.events():
            got.append(ev)
            if isinstance(ev, ToolCalls):
                await p.send_tool_results([ToolResult(ev.calls[0], "pong")])
        await p.close()
        server.close()
        return got

    got = asyncio.run(run())
    assert headers["authorization"] == "Bearer secret-key"
    cfg = seen[0]
    assert cfg["type"] == "config" and cfg["model"] == "phonic-1" and cfg["system_prompt"] == "Be brief."
    assert (cfg["input_format"], cfg["output_format"], cfg["voice_id"]) == ("pcm_16000", "pcm_24000", "sabrina")
    assert cfg["default_language"] == "es"
    assert cfg["tools"][1]["forbid_speech_after_tool_call"] is True and "forbid_speech_after_tool_call" not in cfg["tools"][0]
    tool = cfg["tools"][0]
    assert tool["type"] == "custom_websocket" and tool["tool_schema"]["function"]["strict"] is True
    assert tool["tool_schema"]["function"]["parameters"]["additionalProperties"] is False
    assert [m["type"] for m in seen[1:]] == ["audio_chunk", "add_system_message", "generate_reply", "tool_call_output"]
    assert seen[3]["system_message"] == "greet"
    assert seen[4] == {"type": "tool_call_output", "tool_call_id": "t1", "output": "pong"}
    assert [type(e) for e in got] == [ToolCalls, AudioOut, Transcript, TurnComplete]


# ------------------------------------------------------------------ choosing a provider


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTRING_HOME", str(tmp_path))
    for name in ("AGENTRING_VOICE_PROVIDER", "AGENTRING_VOICE_MODEL", "AGENTRING_VOICE_NAME",
                 "GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY", "PHONIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_resolve_voice_defaults_and_settings(home, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    c = resolve_voice()
    assert (c.provider.name, c.api_key, c.model, c.voice) == ("gemini", "g", "gemini-3.8-live", "Charon")

    assert c.factory()().voice == "Charon"  # Gemini takes no language setting

    (home / ".env").write_text("AGENTRING_VOICE_PROVIDER=grok\nAGENTRING_VOICE_NAME=eve\nXAI_API_KEY=x\n"
                               "AGENTRING_VOICE_LANGUAGE=es\n")
    c = resolve_voice()
    assert (c.provider.name, c.api_key, c.model, c.voice, c.language) == ("grok", "x", "grok-voice-latest", "eve", "es")
    backend = c.factory()()
    assert isinstance(backend, GrokVoice) and (backend.voice, backend.api_key, backend.language) == ("eve", "x", "es")

    # Arguments win; the configured voice belongs to grok, so gemini starts from its own default.
    c = resolve_voice("Gemini", model="gemini-x")
    assert (c.provider.name, c.model, c.voice) == ("gemini", "gemini-x", "Charon")


def test_resolve_voice_errors(home, monkeypatch):
    with pytest.raises(VoiceConfigError, match="GEMINI_API_KEY"):
        resolve_voice()
    with pytest.raises(VoiceConfigError, match="PHONIC_API_KEY"):
        resolve_voice("phonic")
    with pytest.raises(VoiceConfigError, match="gemini, openai, grok, phonic"):
        resolve_voice("siri")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    c = resolve_voice("openai")
    assert isinstance(c.factory()(), OpenAIRealtime) and (c.model, c.voice) == ("gpt-realtime-2.1", "marin")
