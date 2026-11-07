"""Gemini Live (BidiGenerateContent over WebSocket) as a RealtimeBackend."""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets

from .backend import AudioOut, Event, Interrupted, SessionEnding, ToolCalls, ToolResult, TurnComplete, Transcript
from .tools import Tool, ToolCall

log = logging.getLogger("agentring.voice.gemini")

GEMINI_WS = ("wss://generativelanguage.googleapis.com/ws/"
             "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
DEFAULT_MODEL = "gemini-3.8-live"
DEFAULT_VOICE = "Charon"


def function_declarations(tools: list[Tool]) -> list[dict[str, Any]]:
    return [{"name": t.name, "description": t.description, "parameters": _clean_schema(t.parameters)} for t in tools]


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Gemini accepts an OpenAPI subset: drop JSON Schema keys it rejects (MCP servers often send them)."""
    drop = {"$schema", "additionalProperties", "$defs", "definitions", "$id", "title", "default", "examples"}
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k in drop:
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {name: _clean_schema(p) if isinstance(p, dict) else p for name, p in v.items()}
        elif k == "items" and isinstance(v, dict):
            out[k] = _clean_schema(v)
        else:
            out[k] = v
    if out.get("type") == "object" and not out.get("properties"):
        out.pop("required", None)
    return out


class GeminiLive:
    input_rate = 16_000
    output_rate = 24_000
    max_session_s = 15 * 60  # audio-only sessions are cut at 15 min

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, voice: str = DEFAULT_VOICE,
                 url: str = GEMINI_WS, transcripts: bool = True, extra_setup: dict[str, Any] | None = None):
        if not api_key:
            raise ValueError("Gemini Live needs an API key (GEMINI_API_KEY)")
        self.api_key, self.model, self.voice, self.url = api_key, model, voice, url
        self.transcripts = transcripts
        self.extra_setup = extra_setup or {}
        self._ws: Any = None

    async def connect(self, instructions: str, tools: list[Tool]) -> None:
        # The key goes in a header, not the URL, so it can't leak into proxy or error logs.
        self._ws = await websockets.connect(self.url, additional_headers={"x-goog-api-key": self.api_key},
                                            max_size=None)
        setup: dict[str, Any] = {
            "model": self.model if self.model.startswith("models/") else f"models/{self.model}",
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.voice}}}},
            "systemInstruction": {"parts": [{"text": instructions}]},
        }
        if tools:
            setup["tools"] = [{"functionDeclarations": function_declarations(tools)}]
        if self.transcripts:
            setup["inputAudioTranscription"] = {}
            setup["outputAudioTranscription"] = {}
        setup.update(self.extra_setup)
        await self._ws.send(json.dumps({"setup": setup}))
        first = json.loads(await self._ws.recv())
        if "setupComplete" not in first:
            raise RuntimeError(f"Gemini Live setup failed: {str(first)[:300]}")

    async def send_audio(self, pcm: bytes) -> None:
        await self._ws.send(json.dumps({"realtimeInput": {"audio": {
            "data": base64.b64encode(pcm).decode(), "mimeType": f"audio/pcm;rate={self.input_rate}"}}}))

    async def send_text(self, text: str, *, respond: bool, trusted: bool = True) -> None:
        await self._ws.send(json.dumps({"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": respond}}))

    async def send_tool_results(self, results: list[ToolResult]) -> None:
        await self._ws.send(json.dumps({"toolResponse": {"functionResponses": [
            {"id": r.call.id, "name": r.call.name, "response": {"result": r.output}} for r in results]}}))

    async def events(self) -> AsyncIterator[Event]:
        async for raw in self._ws:
            for ev in parse(json.loads(raw)):
                yield ev

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()


def parse(m: dict[str, Any]) -> list[Event]:
    """One server message → events, in the order they should be handled."""
    out: list[Event] = []
    if "toolCall" in m:
        out.append(ToolCalls([ToolCall(fc.get("id"), fc.get("name", ""), fc.get("args") or {})
                              for fc in m["toolCall"].get("functionCalls", [])]))
    if "goAway" in m:
        out.append(SessionEnding(m["goAway"]))
    sc = m.get("serverContent") or {}
    if sc.get("interrupted"):
        out.append(Interrupted())
    for p in (sc.get("modelTurn") or {}).get("parts", []):
        if "inlineData" in p:
            out.append(AudioOut(base64.b64decode(p["inlineData"]["data"])))
    for key, role in (("inputTranscription", "user"), ("outputTranscription", "agent")):
        text = (sc.get(key) or {}).get("text")
        if text:
            out.append(Transcript(role, text))  # type: ignore[arg-type]
    if sc.get("turnComplete") or sc.get("interrupted"):
        out.append(TurnComplete())
    return out
