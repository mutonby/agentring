"""Phonic (speech-to-speech, `phonic-1`) over WebSocket as a RealtimeBackend.

Protocol: https://docs.phonic.ai/asyncapi.yaml. Phonic streams audio all the time,
silence included, so only what it sends while the assistant speaks is passed on
(otherwise the call would think the model never stops talking). Inline tools must
use OpenAI's strict function schemas: every property required, optional ones nullable.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import websockets

from .backend import AudioOut, Event, Interrupted, ToolCalls, ToolResult, TurnComplete, Transcript
from .tools import Tool, ToolCall

log = logging.getLogger("agentring.voice.phonic")

PHONIC_WS = "wss://api.phonic.ai/v1/sts/ws"
DEFAULT_MODEL = "phonic-1"
TOOL_TIMEOUT_MS = 180_000  # Phonic's maximum; slow tools are the session's business
END_CALL = "end_call"  # the session's hang-up tool: Phonic would otherwise keep talking after it

_DROP = {"$schema", "$id", "title", "default", "examples"}


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """A JSON Schema made strict: objects close (`additionalProperties: false`) and list every
    property as required; the ones that were optional also accept null (dropped before the call)."""
    out = {k: v for k, v in schema.items() if k not in _DROP}
    if out.get("type") == "object" or "properties" in out:
        props = out.get("properties") or {}
        required = set(out.get("required") or [])
        fixed = {}
        for name, p in props.items():
            p = strict_schema(p) if isinstance(p, dict) else p
            if name not in required and isinstance(p, dict):
                t = p.get("type")
                if isinstance(t, str):
                    p = {**p, "type": [t, "null"]}
                elif isinstance(t, list) and "null" not in t:
                    p = {**p, "type": [*t, "null"]}
            fixed[name] = p
        out.update(type="object", properties=fixed, required=list(props), additionalProperties=False)
    if isinstance(out.get("items"), dict):
        out["items"] = strict_schema(out["items"])
    return out


class Phonic:
    input_rate = 16_000
    output_rate = 24_000
    max_session_s = None

    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, voice: str | None = None, url: str = PHONIC_WS,
                 language: str | None = None, extra_config: dict[str, Any] | None = None):
        if not api_key:
            raise ValueError("Phonic needs an API key (PHONIC_API_KEY)")
        self.api_key, self.model, self.voice, self.url = api_key, model, voice, url
        self.language = language  # what it speaks before it hears anyone (Phonic's default: en)
        self.extra_config = extra_config or {}
        self._ws: Any = None
        self._speaking = False
        self._optional: dict[str, set[str]] = {}  # tool → arguments that were optional (null = left out)

    def config(self, instructions: str, tools: list[Tool]) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "type": "config", "model": self.model, "system_prompt": instructions,
            "input_format": f"pcm_{self.input_rate}", "output_format": f"pcm_{self.output_rate}",
            "multilingual_mode": "auto", "generate_welcome_message": False,
            "no_input_end_conversation_sec": 600,  # the call decides when to hang up, not Phonic
        }
        if self.voice:
            cfg["voice_id"] = self.voice
        if self.language:
            cfg["default_language"] = self.language.split("-")[0].lower()
        if tools:
            cfg["tools"] = [{"type": "custom_websocket", "execution_mode": "sync",
                             "tool_call_output_timeout_ms": TOOL_TIMEOUT_MS,
                             "tool_schema": {"type": "function", "function": {
                                 "name": t.name, "description": t.description,
                                 "parameters": strict_schema(t.parameters), "strict": True}},
                             **({"forbid_speech_after_tool_call": True} if t.name == END_CALL else {})} for t in tools]
            self._optional = {t.name: set((t.parameters.get("properties") or {})) - set(t.parameters.get("required") or [])
                              for t in tools}
        return {**cfg, **self.extra_config}

    async def connect(self, instructions: str, tools: list[Tool]) -> None:
        self._ws = await websockets.connect(self.url, max_size=None,
                                            additional_headers={"Authorization": f"Bearer {self.api_key}"})
        await self._ws.send(json.dumps(self.config(instructions, tools)))
        while True:
            m = json.loads(await self._ws.recv())
            if m.get("type") == "error":
                raise RuntimeError(f"Phonic setup failed: {json.dumps(m)[:300]}")
            if m.get("type") == "ready_to_start_conversation":
                return

    async def send_audio(self, pcm: bytes) -> None:
        await self._ws.send(json.dumps({"type": "audio_chunk", "audio": base64.b64encode(pcm).decode()}))

    async def send_text(self, text: str, *, respond: bool) -> None:
        # The session's notes are instructions about the call ("X answered, greet them"), not user speech.
        if respond:
            await self._ws.send(json.dumps({"type": "generate_reply", "system_message": text}))
        else:
            await self._ws.send(json.dumps({"type": "add_system_message", "system_message": text}))

    async def send_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            await self._ws.send(json.dumps({"type": "tool_call_output", "tool_call_id": r.call.id, "output": r.output}))

    async def events(self) -> AsyncIterator[Event]:
        async for raw in self._ws:
            for ev in self.parse(json.loads(raw)):
                yield ev

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    def parse(self, m: dict[str, Any]) -> list[Event]:
        t = m.get("type", "")
        if t == "audio_chunk":
            out: list[Event] = []
            if self._speaking and m.get("audio"):
                out.append(AudioOut(base64.b64decode(m["audio"])))
            if m.get("text"):
                out.append(Transcript("agent", m["text"]))
            return out
        if t == "assistant_started_speaking":
            self._speaking = True
        elif t in ("assistant_finished_speaking", "assistant_chose_not_to_respond"):
            self._speaking = False
            return [TurnComplete()]
        elif t == "interrupted_response":
            self._speaking = False
            return [Interrupted(), TurnComplete()]
        elif t == "input_text" and m.get("text"):
            return [Transcript("user", m["text"].strip() + " ")]  # it comes a word or a phrase at a time
        elif t == "tool_call":
            name = m.get("tool_name", "")
            args = {k: v for k, v in (m.get("parameters") or {}).items()
                    if not (v is None and k in self._optional.get(name, ()))}
            return [ToolCalls([ToolCall(m.get("tool_call_id"), name, args)])]
        elif t == "assistant_ended_conversation":
            log.info("Phonic ended the conversation")
        elif t in ("error", "warning"):
            log.warning("Phonic %s: %s", t, json.dumps(m)[:300])
        return []
