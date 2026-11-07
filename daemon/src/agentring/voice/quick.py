"""A ready-made voice agent from a few options (what `agentring call --ai` and `serve --ai` use)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .agents import AgentsConfig, Consultations
from .providers import PROVIDERS, VoiceChoice
from .session import BeforeTool, CallSession, VoiceAgent
from .tools import McpClient, Tool, ToolCall, mcp_tools

DEFAULT_INSTRUCTIONS = (
    "You are the user's AI agent talking to them on the phone. Speak like a person on a call: short, "
    "natural sentences, one idea at a time, in the user's language. Never read out ids, links or paths. "
    "Never claim you did or checked something unless a tool told you so in this call."
)


MISSED_CALL = "Missed call: {reason}"
NO_USER_YET = ("Not run: the user has not said anything on this call yet. Tell them what you would like to do and ask; "
               "call it again once they have answered.")


def after_user_speech(session: CallSession, call: ToolCall) -> str | None:
    """A `before_tool` gate: no tool runs until someone on the call has spoken. For calls whose
    context comes from another program (the MCP `call` tool), so that text alone cannot start
    a tool. It goes by the user's transcript, which every built-in provider gives by default."""
    return None if session.last_user_speech_at else NO_USER_YET


def missed_call_message(relay, call_id: str, reason: str, details: str | None = None) -> str:
    """M4 fallback: what the call was about, as a message the user can answer. Returns msg_id."""
    text = MISSED_CALL.format(reason=reason) + (f"\n\n{details}" if details else "")
    return relay.post_message(text[:4000], reply_to=call_id)["msg_id"]


def read_instructions(value: str | None) -> str:
    """Inline text, or @path to read it from a file."""
    if not value:
        return DEFAULT_INSTRUCTIONS
    if value.startswith("@"):
        return Path(value[1:]).expanduser().read_text(encoding="utf-8")
    return value


@dataclass
class BuiltAgent:
    agent: VoiceAgent
    mcp_clients: list[McpClient]
    consultations: Consultations | None = None

    async def aclose(self, *, wait_answers_s: float | None = 0) -> None:
        """Closes the MCP clients. Agents' answers still on their way get `wait_answers_s`
        (None = as long as they take) to arrive as messages; then they are stopped."""
        if self.consultations is not None:
            if wait_answers_s != 0:
                await self.consultations.drain(wait_answers_s)
            await self.consultations.aclose()
        for c in self.mcp_clients:
            await c.aclose()


async def build_agent(
    voice_ai: VoiceChoice | str,
    *,
    instructions: str | None = None,
    mcp: list[str] | None = None,
    allow: list[str] | None = None,
    agents: AgentsConfig | None = None,
    model: str | None = None,
    voice: str | None = None,
    before_tool: BeforeTool | None = None,
) -> BuiltAgent:
    """A voice AI (`resolve_voice(...)`, or a Gemini API key with `model`/`voice`) with the tools of
    the given MCP servers (only `allow` if given) and of agents.yaml (its MCP servers, and
    `consult_agent` for its agents). `before_tool` runs before every tool (see `VoiceAgent`).
    Close it when done."""
    if isinstance(voice_ai, str):
        gemini = PROVIDERS["gemini"]
        voice_ai = VoiceChoice(gemini, voice_ai, model or gemini.default_model, voice or gemini.default_voice)
    built = BuiltAgent(None, [])  # type: ignore[arg-type]
    tools: list[Tool] = []
    servers = [(url, allow, None) for url in mcp or []]
    servers += [(m.url, m.tools, m.headers) for m in (agents.mcp if agents else [])]
    try:
        for url, only, headers in servers:
            found, client = await mcp_tools(url, allow=only, headers=headers)
            tools += found
            built.mcp_clients.append(client)
    except BaseException:
        await asyncio.shield(built.aclose())
        raise
    if agents and agents.agents:
        built.consultations = Consultations(agents.agents)
        tools.append(built.consultations.tool())
    text = read_instructions(instructions)
    built.agent = VoiceAgent(voice_ai.factory(), text, tools, before_tool=before_tool)
    return built
