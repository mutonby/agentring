"""OpenAI Realtime over WebSocket as a RealtimeBackend; xAI Grok Voice speaks the same protocol.

Function calls are collected until `response.done` and handed over together, so the
tool results go back with a single `response.create` (one at a time would make the
second one fail with "conversation already has an active response").
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote

import websockets

from .backend import AudioOut, Event, Interrupted, ToolCalls, ToolResult, TurnComplete, Transcript
from .tools import Tool, ToolCall

log = logging.getLogger("agentring.voice.openai")

OPENAI_WS = "wss://api.openai.com/v1/realtime"
DEFAULT_MODEL = "gpt-realtime-2.1"
DEFAULT_VOICE = "marin"
DEFAULT_TRANSCRIPTION = "gpt-4o-mini-transcribe"

XAI_WS = "wss://api.x.ai/v1/realtime"
GROK_MODEL = "grok-voice-latest"
GROK_TRANSCRIPTION = "grok-transcribe"


def function_tools(tools: list[Tool]) -> list[dict[str, Any]]:
    return [{"type": "function", "name": t.name, "description": t.description,
             "parameters": {k: v for k, v in t.parameters.items() if k != "$schema"}} for t in tools]


class OpenAIRealtime:
    input_rate = 24_000  # the GA API takes PCM16 at 24 kHz only
    output_rate = 24_000
    max_session_s: float | None = 60 * 60

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, voice: str | None = DEFAULT_VOICE,
                 url: str = OPENAI_WS, transcription_model: str | None = DEFAULT_TRANSCRIPTION, language: str | None = None,
                 turn_detection: dict[str, Any] | None = None, extra_session: dict[str, Any] | None = None):
        if not api_key:
            raise ValueError(f"{type(self).__name__} needs an API key")
        self.api_key, self.model, self.voice, self.url = api_key, model, voice, url
        self.transcription_model = transcription_model
        self.language = language  # a hint for the transcription of what the user says (e.g. "es")
        self.turn_detection = turn_detection or {"type": "server_vad"}
        self.extra_session = extra_session or {}
        self._ws: Any = None
        self._calls: dict[str, ToolCall] = {}  # this response's function calls, by call_id
        self._heard: dict[str, str] = {}  # what the user said since the last turn, by item_id

    def session_config(self, instructions: str, tools: list[Tool]) -> dict[str, Any]:
        audio_in: dict[str, Any] = {"format": {"type": "audio/pcm", "rate": self.input_rate},
                                    "turn_detection": self.turn_detection}
        if self.transcription_model:
            audio_in["transcription"] = {"model": self.transcription_model}
            if self.language:
                audio_in["transcription"]["language"] = self.language.split("-")[0].lower()
        audio_out: dict[str, Any] = {"format": {"type": "audio/pcm", "rate": self.output_rate}}
        if self.voice:
            audio_out["voice"] = self.voice
        session: dict[str, Any] = {"type": "realtime", "model": self.model, "output_modalities": ["audio"],
                                   "instructions": instructions, "audio": {"input": audio_in, "output": audio_out}}
        if tools:
            session["tools"] = function_tools(tools)
            session["tool_choice"] = "auto"
        return session

    async def connect(self, instructions: str, tools: list[Tool]) -> None:
        self._ws = await websockets.connect(f"{self.url}?model={quote(self.model)}", max_size=None,
                                            additional_headers={"Authorization": f"Bearer {self.api_key}"})
        session = {**self.session_config(instructions, tools), **self.extra_session}
        await self._ws.send(json.dumps({"type": "session.update", "session": session}))
        while True:  # session.created comes first, then our update is confirmed (or refused)
            m = json.loads(await self._ws.recv())
            if m.get("type") == "error":
                raise RuntimeError(f"{type(self).__name__} setup failed: {str(m.get('error'))[:300]}")
            if m.get("type") == "session.updated":
                return

    async def send_audio(self, pcm: bytes) -> None:
        await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode()}))

    async def send_text(self, text: str, *, respond: bool) -> None:
        await self._ws.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}))
        if respond:
            await self._ws.send(json.dumps({"type": "response.create"}))

    async def send_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            await self._ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": r.call.id, "output": r.output}}))
        await self._ws.send(json.dumps({"type": "response.create"}))

    async def events(self) -> AsyncIterator[Event]:
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue  # binary transport is never asked for
            for ev in self.parse(json.loads(raw)):
                yield ev

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    def parse(self, m: dict[str, Any]) -> list[Event]:
        """One server message → events, in the order they should be handled."""
        t = m.get("type", "")
        if t in ("response.output_audio.delta", "response.audio.delta"):
            return [AudioOut(base64.b64decode(m["delta"]))]
        if t in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            return [Transcript("agent", m["delta"])] if m.get("delta") else []
        if t == "conversation.item.input_audio_transcription.completed":
            # Grok repeats it with the whole text so far: keep the latest per item, hand it over at the turn's end.
            if m.get("transcript"):
                self._heard[m.get("item_id") or ""] = m["transcript"]
            return []
        if t == "input_audio_buffer.speech_started":
            return [Interrupted()]  # the server cancels its answer itself; drop what is queued to play
        if t == "response.function_call_arguments.done":
            self._add_call(m)
            return []
        if t == "response.done":
            for item in (m.get("response") or {}).get("output") or []:
                if item.get("type") == "function_call":
                    self._add_call(item)
            out: list[Event] = [Transcript("user", " ".join(self._heard.values()))] if self._heard else []
            if self._calls:
                out.append(ToolCalls(list(self._calls.values())))
            self._heard, self._calls = {}, {}
            return [*out, TurnComplete()]
        if t == "error":
            log.warning("%s: %s", type(self).__name__, str(m.get("error"))[:300])
        return []

    def _add_call(self, m: dict[str, Any]) -> None:
        call_id = m.get("call_id")
        if not call_id or call_id in self._calls:
            return
        try:
            args = json.loads(m.get("arguments") or "{}")
        except ValueError:
            args = {}
        self._calls[call_id] = ToolCall(call_id, m.get("name", ""), args if isinstance(args, dict) else {})


class GrokVoice(OpenAIRealtime):
    """xAI Grok Voice: the OpenAI Realtime protocol, with voice and turn detection at the top of the session."""

    max_session_s = None  # no documented limit

    def __init__(self, api_key: str, *, model: str = GROK_MODEL, voice: str | None = None, url: str = XAI_WS,
                 transcription_model: str | None = GROK_TRANSCRIPTION, **kw: Any):
        super().__init__(api_key, model=model, voice=voice, url=url, transcription_model=transcription_model, **kw)

    def session_config(self, instructions: str, tools: list[Tool]) -> dict[str, Any]:
        audio_in: dict[str, Any] = {"format": {"type": "audio/pcm", "rate": self.input_rate}}
        if self.transcription_model:
            audio_in["transcription"] = {"model": self.transcription_model}
            if self.language:
                audio_in["transcription"]["language_hint"] = self.language
        session: dict[str, Any] = {"instructions": instructions, "turn_detection": self.turn_detection,
                                   "audio": {"input": audio_in,
                                             "output": {"format": {"type": "audio/pcm", "rate": self.output_rate}}}}
        if self.voice:
            session["voice"] = self.voice
        if tools:
            session["tools"] = function_tools(tools)
        return session
