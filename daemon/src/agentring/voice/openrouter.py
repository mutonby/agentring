"""OpenRouter as a RealtimeBackend: a cascade of speech-to-text → chat model → text-to-speech.

OpenRouter has no realtime speech-to-speech API, but it serves the three pieces over plain
HTTP with one key: `/audio/transcriptions`, `/chat/completions` (streamed, with tools) and
`/audio/speech` (streamed PCM). This backend chains them, so any chat model on OpenRouter
can be the voice of a call, with the STT and TTS models you pick:

    voice = OpenRouterVoice(api_key, model="anthropic/claude-haiku-5.5", voice="voice_483", language="es")

Turn-taking runs here: a small energy-based voice activity detector (EnergyVAD) cuts the
caller's audio into utterances, and speech over the agent interrupts it. Replies are spoken
sentence by sentence while the model is still writing, so the first word comes after one
transcription, the model's first sentence and the first audio bytes (about 1.5-2.5 s).

With `zdr=True` every request asks OpenRouter to route only to providers with zero data
retention (`provider: {zdr: true, data_collection: "deny"}`); a model without such an
endpoint then fails instead of falling back. `base_url` points the same cascade at any
other server with these OpenAI-compatible endpoints (e.g. a local one).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import wave
from collections.abc import AsyncIterator
from typing import Any

import httpx
import numpy as np

from .backend import AudioOut, Event, Interrupted, ToolCalls, ToolResult, TurnComplete, Transcript
from .phonic import untrusted_note
from .tools import Tool, ToolCall

log = logging.getLogger("agentring.voice.openrouter")

OPENROUTER_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-haiku-5.5"
DEFAULT_STT = "mistralai/voxtral-mini-transcribe-3-0"
DEFAULT_TTS = "mistralai/voxtral-mini-tts-3-0"
DEFAULT_VOICE = "voice_478"  # a Voxtral 3 preset; the TTS model needs an explicit voice

# Added to the instructions: the model writes text that is read out loud on a phone.
SPOKEN_STYLE = ("You are speaking on a phone call: everything you write is read out loud. Answer in short, "
                "natural spoken sentences. No markdown, lists, emojis, URLs or code.")

_SENTENCE_END = re.compile(r"[.!?…:;](?=\s)|\n")


class EnergyVAD:
    """Cuts PCM16 mono into utterances by loudness, against an adaptive noise floor.

    `feed` returns ("start", None) when speech begins (after `start_ms` of it) and
    ("end", pcm) when it has been quiet for `end_ms`, with the utterance (plus `preroll_ms`
    from before the start, so the first syllable isn't lost). Utterances shorter than
    `min_speech_ms` of speech (a cough, a click) end with ("discard", None).
    """

    def __init__(self, rate: int, *, frame_ms: int = 20, start_ms: int = 80, end_ms: int = 550,
                 preroll_ms: int = 300, min_speech_ms: int = 250, min_rms: float = 350.0, ratio: float = 3.0,
                 max_utterance_s: float = 30.0):
        self.rate = rate
        self.frame = rate * frame_ms // 1000 * 2  # bytes
        self.frame_ms = frame_ms
        self.start_frames = max(1, start_ms // frame_ms)
        self.end_frames = max(1, end_ms // frame_ms)
        self.preroll_frames = preroll_ms // frame_ms
        self.min_speech_frames = max(1, min_speech_ms // frame_ms)
        self.max_frames = int(max_utterance_s * 1000 / frame_ms)
        self.min_rms, self.ratio = min_rms, ratio
        self.floor = min_rms / ratio
        self._pending = bytearray()
        self._recent: list[bytes] = []  # the last frames before speech (preroll and onset)
        self._utt: list[bytes] = []
        self._loud_run = 0
        self._quiet_run = 0
        self._speech_frames = 0
        self.speaking = False

    def threshold(self) -> float:
        return max(self.min_rms, self.floor * self.ratio)

    def feed(self, pcm: bytes) -> list[tuple[str, bytes | None]]:
        self._pending += pcm
        out: list[tuple[str, bytes | None]] = []
        while len(self._pending) >= self.frame:
            frame = bytes(self._pending[: self.frame])
            del self._pending[: self.frame]
            ev = self._frame(frame)
            if ev:
                out.append(ev)
        return out

    def _frame(self, frame: bytes) -> tuple[str, bytes | None] | None:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        loud = rms > self.threshold()
        if not self.speaking:
            if not loud:  # the floor follows the room while nobody speaks (falls fast, rises slowly)
                self.floor = rms if rms < self.floor else 0.95 * self.floor + 0.05 * rms
            self._recent.append(frame)
            self._recent = self._recent[-(self.preroll_frames + self.start_frames):]
            self._loud_run = self._loud_run + 1 if loud else 0
            if self._loud_run >= self.start_frames:
                self.speaking = True
                self._utt = list(self._recent)
                self._recent = []
                self._speech_frames = self._loud_run
                self._quiet_run = 0
                return ("start", None)
            return None
        self._utt.append(frame)
        if loud:
            self._speech_frames += 1
            self._quiet_run = 0
        else:
            self._quiet_run += 1
        if self._quiet_run >= self.end_frames or len(self._utt) >= self.max_frames:
            pcm = b"".join(self._utt)
            enough = self._speech_frames >= self.min_speech_frames
            self.speaking, self._utt, self._loud_run, self._speech_frames = False, [], 0, 0
            return ("end", pcm) if enough else ("discard", None)
        return None


def wav_bytes(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def chat_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description,
        "parameters": {k: v for k, v in t.parameters.items() if k != "$schema"}}} for t in tools]


def split_sentences(text: str, min_chars: int = 12) -> tuple[list[str], str]:
    """Whole sentences ready to speak, and the rest still being written."""
    out: list[str] = []
    start = 0
    for m in _SENTENCE_END.finditer(text):
        end = m.end()
        if len(text[start:end].strip()) >= min_chars:
            out.append(text[start:end].strip())
            start = end
    return out, text[start:]


class OpenRouterVoice:
    input_rate = 16_000
    output_rate = 24_000  # PCM16 mono from /audio/speech (checked against the content-type when given)
    max_session_s: float | None = None

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, voice: str | None = DEFAULT_VOICE,
                 language: str | None = None, stt_model: str = DEFAULT_STT, tts_model: str = DEFAULT_TTS,
                 base_url: str = OPENROUTER_URL, zdr: bool = False, vad: EnergyVAD | None = None,
                 extra_body: dict[str, Any] | None = None, client: httpx.AsyncClient | None = None):
        if not api_key:
            raise ValueError("OpenRouterVoice needs an API key")
        self.api_key, self.model, self.voice = api_key, model, voice or DEFAULT_VOICE
        self.language = language.split("-")[0].lower() if language else None
        self.stt_model, self.tts_model = stt_model, tts_model
        self.base_url = base_url.rstrip("/")
        self.zdr = zdr
        self.extra_body = extra_body or {}
        self.vad = vad or EnergyVAD(self.input_rate)
        self._client = client
        self._own_client = client is None
        self._events: asyncio.Queue[Event | None] = asyncio.Queue()
        self._messages: list[dict[str, Any]] = []
        self._tools: list[Tool] = []
        self._lock = asyncio.Lock()  # one reply at a time, in order
        self._reply: asyncio.Task[None] | None = None
        self._pending_tools: dict[str, ToolCall] = {}  # asked for, results not back yet
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    # ------------------------------------------------------------ RealtimeBackend

    async def connect(self, instructions: str, tools: list[Tool]) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10), headers={
                "Authorization": f"Bearer {self.api_key}", "X-Title": "agentring"})
        self._tools = list(tools)
        self._messages = [{"role": "system", "content": f"{instructions}\n\n{SPOKEN_STYLE}"}]

    async def send_audio(self, pcm: bytes) -> None:
        for kind, utterance in self.vad.feed(pcm):
            if kind == "start":
                self._interrupt()
            elif kind == "end" and utterance:
                self._spawn(self._on_utterance(utterance))

    async def send_text(self, text: str, *, respond: bool, trusted: bool = True) -> None:
        self._add_user(text if trusted else untrusted_note(text))
        if respond:
            self._start_reply()

    async def send_tool_results(self, results: list[ToolResult]) -> None:
        late: list[ToolResult] = []
        for r in results:
            if r.call.id and self._pending_tools.pop(r.call.id, None) is not None:
                self._messages.append({"role": "tool", "tool_call_id": r.call.id, "content": r.output})
            else:
                late.append(r)  # its turn already went on without it (the caller spoke meanwhile)
        for r in late:
            self._add_user(untrusted_note(f"Result of {r.call.name}: {r.output}"))
        self._start_reply()

    async def events(self) -> AsyncIterator[Event]:
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for t in list(self._tasks):
            t.cancel()
        if self._own_client and self._client is not None:
            await self._client.aclose()
        self._events.put_nowait(None)

    # ------------------------------------------------------------ turns

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _interrupt(self) -> None:
        self._events.put_nowait(Interrupted())
        if self._reply and not self._reply.done():
            self._reply.cancel()

    def _add_user(self, text: str) -> None:
        if self._pending_tools:
            # Chat APIs want a tool's result right after the call that asked for it: close the open
            # calls before the conversation goes on, their real results come later as a note.
            for call_id, call in self._pending_tools.items():
                self._messages.append({"role": "tool", "tool_call_id": call_id,
                                       "content": f"{call.name} is still running; its result will come as a note."})
            self._pending_tools = {}
        last = self._messages[-1] if self._messages else None
        if last and last["role"] == "user" and isinstance(last.get("content"), str):
            last["content"] += f"\n{text}"
        else:
            self._messages.append({"role": "user", "content": text})

    def _start_reply(self) -> None:
        self._reply = self._spawn(self._reply_turn())

    async def _on_utterance(self, pcm: bytes) -> None:
        try:
            text = (await self.transcribe(pcm)).strip()
        except Exception as e:
            log.warning("transcription failed: %s", str(e)[:300])
            return
        if not text:
            return
        self._events.put_nowait(Transcript("user", text))
        self._add_user(text)
        self._start_reply()

    async def _reply_turn(self) -> None:
        async with self._lock:
            spoken: list[str] = []
            calls: list[ToolCall] = []
            try:
                calls = await self._generate(spoken)
            except asyncio.CancelledError:
                if spoken:  # what the caller heard before talking over it
                    self._messages.append({"role": "assistant", "content": " ".join(spoken)})
                self._events.put_nowait(TurnComplete())
                raise
            except Exception as e:
                log.warning("reply failed: %s", str(e)[:300])
            if calls:
                self._events.put_nowait(ToolCalls(calls))
            self._events.put_nowait(TurnComplete())

    async def _generate(self, spoken: list[str]) -> list[ToolCall]:
        """Streams one answer, speaking each sentence as soon as it is complete. Returns the tool
        calls it asked for (they are in the history, waiting for their results)."""
        body: dict[str, Any] = {"model": self.model, "messages": self._messages, "stream": True, **self.extra_body}
        if self._tools:
            body["tools"] = chat_tools(self._tools)
        self._add_policy(body)
        text, rest = "", ""
        partial: dict[int, dict[str, Any]] = {}
        sentences: asyncio.Queue[str | None] = asyncio.Queue()
        speaker = asyncio.create_task(self._speak(sentences, spoken))
        try:
            assert self._client is not None
            async with self._client.stream("POST", f"{self.base_url}/chat/completions", json=body) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"chat {r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}")
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise RuntimeError(f"chat error: {str(chunk['error'])[:300]}")
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            text += delta["content"]
                            done, rest = split_sentences(rest + delta["content"])
                            for s in done:
                                sentences.put_nowait(s)
                        for tc in delta.get("tool_calls") or []:
                            p = partial.setdefault(tc.get("index", 0), {"id": None, "name": "", "args": ""})
                            p["id"] = tc.get("id") or p["id"]
                            fn = tc.get("function") or {}
                            p["name"] += fn.get("name") or ""
                            p["args"] += fn.get("arguments") or ""
            if rest.strip():
                sentences.put_nowait(rest.strip())
            sentences.put_nowait(None)
            await speaker
        except BaseException:
            speaker.cancel()
            raise
        calls: list[ToolCall] = []
        for i in sorted(partial):
            p = partial[i]
            try:
                args = json.loads(p["args"] or "{}")
            except ValueError:
                args = {}
            calls.append(ToolCall(p["id"] or f"call_{i}", p["name"], args if isinstance(args, dict) else {}))
        msg: dict[str, Any] = {"role": "assistant", "content": text or None}
        if calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function", "function": {
                "name": c.name, "arguments": json.dumps(c.args, ensure_ascii=False)}} for c in calls]
            self._pending_tools = {c.id: c for c in calls if c.id}
        if text or calls:
            self._messages.append(msg)
        return calls

    async def _speak(self, sentences: asyncio.Queue[str | None], spoken: list[str]) -> None:
        while (s := await sentences.get()) is not None:
            await self.synthesize(s)
            spoken.append(s)
            self._events.put_nowait(Transcript("agent", s + " "))

    # ------------------------------------------------------------ HTTP pieces

    def _add_policy(self, body: dict[str, Any]) -> None:
        if self.zdr:
            body["provider"] = {**body.get("provider", {}), "zdr": True, "data_collection": "deny"}

    async def transcribe(self, pcm: bytes) -> str:
        body: dict[str, Any] = {"model": self.stt_model, "input_audio": {
            "data": base64.b64encode(wav_bytes(pcm, self.input_rate)).decode(), "format": "wav"}}
        if self.language:
            body["language"] = self.language
        self._add_policy(body)
        assert self._client is not None
        r = await self._client.post(f"{self.base_url}/audio/transcriptions", json=body)
        if r.status_code != 200:
            raise RuntimeError(f"transcription {r.status_code}: {r.text[:300]}")
        return str(r.json().get("text") or "")

    async def synthesize(self, text: str) -> None:
        """Speaks `text`: PCM chunks go out as AudioOut while the response streams in."""
        body: dict[str, Any] = {"model": self.tts_model, "input": text, "voice": self.voice, "response_format": "pcm"}
        self._add_policy(body)
        assert self._client is not None
        async with self._client.stream("POST", f"{self.base_url}/audio/speech", json=body) as r:
            if r.status_code != 200:
                raise RuntimeError(f"speech {r.status_code}: {(await r.aread()).decode(errors='replace')[:300]}")
            rate = re.search(r"rate=(\d+)", r.headers.get("content-type", ""))
            if rate and int(rate.group(1)) != self.output_rate:
                raise RuntimeError(f"speech came at {rate.group(1)} Hz, expected {self.output_rate}")
            odd = b""
            async for chunk in r.aiter_bytes():
                chunk = odd + chunk
                cut = len(chunk) - len(chunk) % 2  # whole 16-bit samples only
                odd = chunk[cut:]
                if cut:
                    self._events.put_nowait(AudioOut(chunk[:cut]))
