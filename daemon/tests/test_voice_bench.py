"""`agentring voice bench` against a fake OpenAI-Realtime server: timing, tools and scoring."""

from __future__ import annotations

import asyncio
import base64
import json
import wave

import numpy as np
import pytest

pytest.importorskip("livekit")

import websockets  # noqa: E402

from agentring.voice import bench  # noqa: E402
from agentring.voice.openai_realtime import LocalRealtime  # noqa: E402

LOUD = base64.b64encode((np.ones(2400, dtype=np.int16) * 3000).tobytes()).decode()  # 0.1 s of "voice"
QUIET = base64.b64encode(np.zeros(2400, dtype=np.int16).tobytes()).decode()


def _fake_server(talk_first: bool):
    """Answers the first audio it gets with a tool call (optionally saying something first),
    then speaks once the tool result is in."""

    async def handler(ws):
        await ws.send(json.dumps({"type": "session.created"}))
        await ws.recv()
        await ws.send(json.dumps({"type": "session.updated"}))
        answered = False
        async for raw in ws:
            m = json.loads(raw)
            if m["type"] == "input_audio_buffer.append" and not answered:
                answered = True
                await asyncio.sleep(0.5)  # "the user is still speaking"; audio before the end doesn't count
                await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": QUIET}))
                if talk_first:
                    await asyncio.sleep(0.6)
                    await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": LOUD}))
                await ws.send(json.dumps({"type": "response.function_call_arguments.done", "call_id": "c1",
                                          "name": "consult_agent", "arguments": json.dumps({"task": "tests"})}))
                await ws.send(json.dumps({"type": "response.done", "response": {"output": []}}))
            elif m["type"] == "response.create":
                await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": LOUD}))
                await ws.send(json.dumps({"type": "response.output_audio_transcript.delta", "delta": "3 fail."}))
                await ws.send(json.dumps({"type": "response.done", "response": {"output": []}}))

    return handler


@pytest.mark.parametrize("talk_first", [True, False])
def test_session_times_the_first_word_and_the_tools(talk_first):
    async def run():
        server = await websockets.serve(_fake_server(talk_first), "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            pcm = np.zeros(24000 // 5, dtype=np.int16).tobytes()  # 0.2 s of "speech"
            return await bench.run_session(LocalRealtime(url=url), "consult", pcm,
                                           tools=bench.bench_tools(consult_delay_s=0.5), quiet_s=0.8)
        finally:
            server.close()

    s = asyncio.run(run())
    assert not s.error and s.tools == ["consult_agent"] and s.tools_ok
    assert s.spoke_before_tool is talk_first
    assert s.first_voice_s is not None
    assert s.agent == "3 fail."


def test_scoring_and_summary():
    assert bench.tools_ok("chat", []) and not bench.tools_ok("chat", ["get_time"])
    assert bench.tools_ok("chain", ["get_build_status", "create_issue"])
    assert not bench.tools_ok("chain", ["get_build_status"]) and not bench.tools_ok("consult", [])
    S = bench.Session
    summ = bench.summary([S("chat", 0, 1.2, tools_ok=True), S("chat", 1, 1.6, tools_ok=True), S("chat", 2, error="x"),
                          S("consult", 0, 2.0, ["consult_agent"], True, True), S("consult", 1, None, ["consult_agent"], False)])
    assert summ["chat"] == {"first_voice_s": 1.4, "tools_ok": "2/3", "spoke_before_tool": None, "errors": 1}
    assert summ["consult"]["spoke_before_tool"] == "1/2" and summ["consult"]["tools_ok"] == "1/2"


def test_audio_helpers(tmp_path):
    f = tmp_path / "chat.wav"
    with wave.open(str(f), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.full(3200, 1000, dtype=np.int16).tobytes())  # 0.1 s stereo
    a, rate = bench.read_wav(f)
    assert rate == 16000 and len(a) == 1600
    assert len(bench.resample(a, 16000, 24000)) == 2400 * 2
    assert bench.voiced(bench.resample(a, 16000, 24000)) and not bench.voiced(b"\0" * 400)
    with pytest.raises(FileNotFoundError, match="time.wav"):
        bench.prompt_files(tmp_path)
    assert "let me check" in bench.bench_instructions()
