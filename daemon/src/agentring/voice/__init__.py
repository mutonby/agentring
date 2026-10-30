"""Voice for agentring calls (extra: agentring[voice]).

    from agentring.voice import VoiceAgent, CallInfo, GeminiLive, Tool

    agent = VoiceAgent(lambda: GeminiLive(api_key), "You are my assistant…", tools=[Tool(...)])
    # or OpenAIRealtime / GrokVoice / Phonic, or resolve_voice("grok").factory()
    result = await agent.run(CallInfo(call_id, reason), join, relay=client)

Names load lazily, so `agentring.voice.inbound` (relay side only) works without LiveKit installed.
"""

from importlib import import_module
from typing import Any

_WHERE = {
    "AgentsConfig": "agents", "CommandAgent": "agents", "ConsultPrompts": "agents", "Consultations": "agents",
    "load_agents": "agents",
    "RealtimeBackend": "backend", "ToolResult": "backend",
    "GeminiLive": "gemini",
    "GrokVoice": "openai_realtime", "OpenAIRealtime": "openai_realtime",
    "Phonic": "phonic",
    "PROVIDERS": "providers", "VoiceChoice": "providers", "VoiceConfigError": "providers", "resolve_voice": "providers",
    "accept_incoming": "inbound", "is_incoming_call": "inbound",
    "CallInfo": "session", "CallResult": "session", "CallSession": "session", "Prompts": "session",
    "VoiceAgent": "session",
    "McpClient": "tools", "Tool": "tools", "ToolCall": "tools", "current_session": "tools", "mcp_tools": "tools",
}
__all__ = sorted(_WHERE)


def __getattr__(name: str) -> Any:
    if name not in _WHERE:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{_WHERE[name]}", __name__), name)
