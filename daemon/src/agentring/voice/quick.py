"""A ready-made voice agent from a few options (what `agentring call --ai` and `serve --ai` use)."""

from __future__ import annotations

import asyncio
import json
import time
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


CONFIRM_FIRST = ("Not run yet. Tell the user in one short sentence exactly what you are about to do ({tool}: {args}) "
                 "and ask them to confirm. Call it again with exactly the same arguments only after they clearly say "
                 "yes; if they say no or hesitate, don't.")
NOT_CONFIRMED = "Not run: the user hasn't answered since you asked them. Wait for their yes, then call it again."
CONFIRM_MARGIN_S = 1.0  # speech transcribed this soon after the question was said before it


def confirm_first(session: CallSession, call: ToolCall) -> str | None:
    """A `before_tool` gate: a tool runs only once the user has heard what it will do and spoken
    after that. The first request is answered with "read it back and ask"; the same request runs
    when it comes again after the user has said something. Text from another program (an agent's
    answer, the context of an MCP call, a tool description) can ask for a tool but can't speak on
    the call, so it can't make one run on its own."""
    if not session.last_user_speech_at:
        return NO_USER_YET
    key = f"{call.name} {json.dumps(call.args, sort_keys=True, ensure_ascii=False)}"
    asked = session.confirmations.get(key)
    if asked is None:
        session.confirmations[key] = time.time()
        return CONFIRM_FIRST.format(tool=call.name, args=json.dumps(call.args, ensure_ascii=False)[:300])
    if session.last_user_speech_at < asked + CONFIRM_MARGIN_S:
        return NOT_CONFIRMED
    del session.confirmations[key]
    return None


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
    `consult_agent` for its agents). `before_tool` runs before every tool (see `VoiceAgent`); by
    default `confirm_first`, or `after_user_speech` when agents.yaml says `confirm: false`.
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
    if before_tool is None:
        before_tool = after_user_speech if agents is not None and not agents.confirm else confirm_first
    built.agent = VoiceAgent(voice_ai.factory(), voice_ai.with_language(text), tools, before_tool=before_tool)
    return built
