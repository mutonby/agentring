"""`agentring voice bench`: the phone-call test from the agentring blog, against any voice provider.

Four spoken prompts are streamed in real time into the provider, the way a caller's
microphone would send them (then open-mic silence), with the same four demo tools every time:

    chat     plain conversation, no tools
    time     one quick tool (get_time)
    chain    two tools in a row (get_build_status, then create_issue)
    consult  a tool that takes 3 seconds (consult_agent)

For each session it records the time from the end of the user's speech to the first audio
with voice in it (silence doesn't count: some providers stream it all the time), the tools
called, and whether the model said something before a tool answered. The prompts are spoken
by the system's text-to-speech (`say` on macOS, `espeak-ng` elsewhere) unless you pass your
own recordings, so the numbers compare providers on one machine, not with the blog's.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import statistics
import subprocess
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .backend import AudioOut, RealtimeBackend, ToolCalls, ToolResult, Transcript
from .quick import DEFAULT_INSTRUCTIONS
from .session import Prompts
from .tools import Tool, ToolCall, result_text

SCENARIOS: dict[str, list[str]] = {
    "chat": [],
    "time": ["get_time"],
    "chain": ["get_build_status", "create_issue"],
    "consult": ["consult_agent"],
}

PROMPTS = {
    "en": {
        "chat": "Hi, how are you? Tell me in one sentence what you can do for me.",
        "time": "What time is it right now?",
        "chain": "Check how the last build went and, if it failed, open an issue with the error.",
        "consult": "Ask the agent how many tests are failing right now.",
    },
    "es": {
        "chat": "Hola, ¿qué tal? Dime en una frase qué puedes hacer por mí.",
        "time": "¿Qué hora es ahora mismo?",
        "chain": "Mira cómo ha ido el último build y, si ha fallado, abre una incidencia con el error.",
        "consult": "Pregúntale al agente cuántos tests están fallando ahora mismo.",
    },
}
SAY_VOICES = {"en": "Samantha", "es": "Mónica"}  # macOS voices that ship with the system
CONSULT_DELAY_S = 3.0
VOICED_RMS = 150  # PCM16 RMS above this counts as speech, not silence or noise


def bench_tools(consult_delay_s: float = CONSULT_DELAY_S) -> list[Tool]:
    async def consult(task: str) -> str:
        await asyncio.sleep(consult_delay_s)
        return "3 tests are failing right now, all in test_relay.py, because of a WebSocket timeout."

    def create_issue(title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        return {"url": "https://github.com/acme/app/issues/17", "title": title}

    return [
        Tool("get_time", "Current local time.", lambda: {"time": time.strftime("%H:%M")}),
        Tool("get_build_status", "Status of the latest CI build.",
             lambda: {"status": "failed", "error": "test_ws_resume: AssertionError: expected seq 42, got 41"}),
        Tool("create_issue", "Opens an issue on GitHub.", create_issue, {"type": "object", "properties": {
            "title": {"type": "string"}, "body": {"type": "string"},
            "labels": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "body"]}),
        Tool("consult_agent", "Asks the user's coding agent something (takes a few seconds).", consult,
             {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}),
    ]


def bench_instructions() -> str:
    """What a real `agentring call --ai` tells the model, including the hint added when there are tools."""
    return f"{DEFAULT_INSTRUCTIONS}\n\n{Prompts().before_tools}"


# ------------------------------------------------------------------ audio


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """A PCM16 WAV file as mono int16 samples and its rate."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: needs 16-bit PCM")
        rate, channels = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    if channels > 1:
        a = a.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return a, rate


def resample(a: np.ndarray, rate: int, to: int) -> bytes:
    if rate == to:
        return a.astype(np.int16).tobytes()
    n = int(len(a) * to / rate)
    x = np.interp(np.arange(n) * (rate / to), np.arange(len(a)), a.astype(np.float32))
    return x.astype(np.int16).tobytes()


def voiced(pcm: bytes) -> bool:
    a = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32)
    return a.size > 0 and float(np.sqrt(np.mean(a * a))) > VOICED_RMS


def speak_prompts(lang: str, out: Path) -> dict[str, Path]:
    """The four prompts as WAV files, spoken by the system's text-to-speech."""
    texts = PROMPTS[lang]
    files = {}
    say, espeak = shutil.which("say"), shutil.which("espeak-ng") or shutil.which("espeak")
    for name, text in texts.items():
        f = out / f"{name}.wav"
        if say:
            cmd = [say, "-v", SAY_VOICES[lang], "--file-format=WAVE", "--data-format=LEI16@24000", "-o", str(f), text]
        elif espeak:
            cmd = [espeak, "-v", lang, "-w", str(f), text]
        else:
            raise RuntimeError("no text-to-speech found (macOS `say` or espeak-ng): pass --prompts with your own "
                               "recordings (chat.wav, time.wav, chain.wav, consult.wav)")
        subprocess.run(cmd, check=True, capture_output=True)
        files[name] = f
    return files


def prompt_files(directory: Path) -> dict[str, Path]:
    files = {name: directory / f"{name}.wav" for name in SCENARIOS}
    missing = [f.name for f in files.values() if not f.exists()]
    if missing:
        raise FileNotFoundError(f"{directory}: missing {', '.join(missing)}")
    return files


# ------------------------------------------------------------------ one session


@dataclass
class Session:
    scenario: str
    run: int
    first_voice_s: float | None = None  # end of the user's speech → first audio with voice
    tools: list[str] = field(default_factory=list)
    spoke_before_tool: bool | None = None  # said something before the first tool answered (None: no tool)
    tools_ok: bool = False
    agent: str = ""
    user: str = ""
    error: str = ""
    audio: bytes = field(default=b"", repr=False)


def tools_ok(scenario: str, called: list[str]) -> bool:
    return set(called) == set(SCENARIOS[scenario]) and (bool(called) or not SCENARIOS[scenario])


async def run_session(backend: RealtimeBackend, scenario: str, pcm: bytes, *, run: int = 0,
                      tools: list[Tool] | None = None, instructions: str | None = None,
                      max_s: float = 40, quiet_s: float = 3) -> Session:
    """Streams `pcm` (PCM16 at the backend's input rate) like a microphone and watches the answer."""
    tools = tools if tools is not None else bench_tools()
    by_name = {t.name: t for t in tools}
    s = Session(scenario, run)
    await backend.connect(instructions or bench_instructions(), tools)
    chunk = backend.input_rate * 2 // 50  # 20 ms
    t_end: float | None = None
    first_voice_at: float | None = None
    first_tool_done: float | None = None
    last_event = time.monotonic()
    pending = 0
    stop = asyncio.Event()

    async def mic() -> None:
        nonlocal t_end
        frames = [b"\0" * chunk] * 10 + [pcm[i:i + chunk].ljust(chunk, b"\0") for i in range(0, len(pcm), chunk)]
        start = time.monotonic()
        for n, f in enumerate(frames):
            await backend.send_audio(f)
            await asyncio.sleep(max(0.0, start + (n + 1) * 0.02 - time.monotonic()))
        t_end = time.monotonic()
        n = 0
        while not stop.is_set():  # open mic: silence in real time
            await backend.send_audio(b"\0" * chunk)
            n += 1
            await asyncio.sleep(max(0.0, t_end + n * 0.02 - time.monotonic()))

    async def answer(calls: list[ToolCall]) -> None:
        nonlocal pending, first_tool_done
        results = []
        for c in calls:
            tool = by_name.get(c.name)
            try:
                out = result_text(await tool.call(c.args)) if tool else f"No such tool: {c.name}"
            except Exception as e:
                out = f"Error: {e}"
            results.append(ToolResult(c, out))
        first_tool_done = first_tool_done or time.monotonic()
        try:
            await backend.send_tool_results(results)
        except Exception:
            pass  # the session is closing
        finally:
            pending -= 1

    async def recv() -> None:
        nonlocal last_event, pending, first_voice_at
        async for ev in backend.events():
            now = time.monotonic()
            last_event = now
            if t_end is None:
                continue  # the user is still talking
            if isinstance(ev, AudioOut):
                s.audio += ev.pcm
                if first_voice_at is None and voiced(ev.pcm):
                    first_voice_at = now
                    s.first_voice_s = round(now - t_end, 2)
            elif isinstance(ev, Transcript):
                if ev.role == "agent":
                    s.agent += ev.text
                else:
                    s.user += ev.text + " "
            elif isinstance(ev, ToolCalls):
                s.tools += [c.name for c in ev.calls]
                pending += 1
                asyncio.create_task(answer(ev.calls))

    tasks = [asyncio.create_task(mic()), asyncio.create_task(recv())]
    try:
        while True:
            await asyncio.sleep(0.2)
            if tasks[1].done():
                if tasks[1].exception():
                    s.error = repr(tasks[1].exception())[:300]
                break
            now = time.monotonic()
            if t_end is None:
                continue
            if now - t_end > max_s:
                s.error = s.error or "timeout"
                break
            waiting_for_tools = bool(SCENARIOS[scenario]) and not s.tools and now - t_end < 15
            if s.audio and pending == 0 and now - last_event > quiet_s and not waiting_for_tools:
                break
            if not s.audio and now - t_end > 20:
                s.error = "no answer"
                break
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await backend.close()
        except Exception:
            pass
    if s.tools:  # did the caller hear anything before the first tool answered?
        s.spoke_before_tool = first_voice_at is not None and (first_tool_done is None or first_voice_at < first_tool_done)
    s.tools_ok = tools_ok(scenario, s.tools)
    s.agent, s.user = " ".join(s.agent.split()), " ".join(s.user.split())
    return s


# ------------------------------------------------------------------ the whole run


async def run_bench(factory: Callable[[], RealtimeBackend], prompts: dict[str, Path], *, runs: int = 3,
                    out: Path | None = None, instructions: str | None = None,
                    on_session: Callable[[Session], None] | None = None) -> list[Session]:
    sessions = []
    for scenario in SCENARIOS:
        a, rate = read_wav(prompts[scenario])
        for i in range(runs):
            backend = factory()
            try:
                s = await run_session(backend, scenario, resample(a, rate, backend.input_rate), run=i,
                                      instructions=instructions)
            except Exception as e:  # a failed session is a data point too
                s = Session(scenario, i, error=repr(e)[:300])
            if out is not None:
                with wave.open(str(out / f"{scenario}-{i}.wav"), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(backend.output_rate)
                    w.writeframes(s.audio)
            sessions.append(s)
            if on_session:
                on_session(s)
    if out is not None:
        with open(out / "results.jsonl", "w", encoding="utf-8") as f:
            for s in sessions:
                d = asdict(s)
                d.pop("audio")
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
    return sessions


def summary(sessions: list[Session]) -> dict[str, dict[str, Any]]:
    """Per scenario: median first voice, right tools (n/total), how often it spoke before a tool answered."""
    out: dict[str, dict[str, Any]] = {}
    for scenario in SCENARIOS:
        ss = [s for s in sessions if s.scenario == scenario]
        if not ss:
            continue
        times = [s.first_voice_s for s in ss if s.first_voice_s is not None]
        with_tools = [s for s in ss if s.spoke_before_tool is not None]
        out[scenario] = {
            "first_voice_s": round(statistics.median(times), 2) if times else None,
            "tools_ok": f"{sum(s.tools_ok for s in ss)}/{len(ss)}",
            "spoke_before_tool": f"{sum(bool(s.spoke_before_tool) for s in with_tools)}/{len(with_tools)}"
            if SCENARIOS[scenario] else None,
            "errors": sum(bool(s.error) for s in ss),
        }
    return out


def temp_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="agentring-bench-"))
