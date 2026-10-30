"""The plug point for realtime speech-to-speech models (Gemini Live, OpenAI Realtime, Grok Voice, Phonic).

A backend turns a model's wire protocol into a few plain events. The call session
(session.py) never sees a provider's JSON, so adding a provider means one class.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from .tools import Tool, ToolCall


@dataclass
class AudioOut:
    """Model speech, PCM16 mono at the backend's `output_rate`."""

    pcm: bytes


@dataclass
class Interrupted:
    """The user talked over the model: drop whatever is queued to play."""


@dataclass
class Transcript:
    """A piece of transcript (deltas: concatenate until TurnComplete)."""

    role: Literal["user", "agent"]
    text: str


@dataclass
class TurnComplete:
    pass


@dataclass
class ToolCalls:
    calls: list[ToolCall]


@dataclass
class SessionEnding:
    """The provider will close the session soon (e.g. Gemini's goAway)."""

    detail: Any = None


Event = AudioOut | Interrupted | Transcript | TurnComplete | ToolCalls | SessionEnding


@dataclass
class ToolResult:
    call: ToolCall
    output: str


class RealtimeBackend(Protocol):
    input_rate: int  # what the model listens to (Hz, PCM16 mono)
    output_rate: int  # what it speaks
    max_session_s: float | None  # provider limit for one audio session, if any

    async def connect(self, instructions: str, tools: list[Tool]) -> None: ...

    async def send_audio(self, pcm: bytes) -> None: ...

    async def send_text(self, text: str, *, respond: bool) -> None:
        """A text note in the conversation (who joined, why we're calling…). With
        `respond`, the model answers it out loud; otherwise it just knows it."""

    async def send_tool_results(self, results: list[ToolResult]) -> None: ...

    def events(self) -> AsyncIterator[Event]: ...

    async def close(self) -> None: ...
