"""The voice providers agentring ships, by name: what `--provider` and AGENTRING_VOICE_PROVIDER pick.

    choice = resolve_voice("grok")          # or None: AGENTRING_VOICE_PROVIDER, else gemini
    agent = VoiceAgent(choice.factory(), "You are …", tools)

Defaults come from the environment or ~/.agentring/.env, so the CLI and the MCP server
agree: AGENTRING_VOICE_PROVIDER, AGENTRING_VOICE_MODEL, AGENTRING_VOICE_NAME, the
provider's own key (GEMINI_API_KEY, OPENAI_API_KEY, XAI_API_KEY, PHONIC_API_KEY) and
AGENTRING_VOICE_LANGUAGE (e.g. "es": Phonic speaks it before it hears anyone; OpenAI
and Grok use it to transcribe the user; Gemini follows the instructions).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import secret

DEFAULT_PROVIDER = "gemini"


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    key_env: str
    default_model: str
    default_voice: str | None  # None: the provider's own default
    takes_language: bool = True

    def backend_class(self) -> Any:
        if self.name == "gemini":
            from .gemini import GeminiLive
            return GeminiLive
        if self.name == "openai":
            from .openai_realtime import OpenAIRealtime
            return OpenAIRealtime
        if self.name == "grok":
            from .openai_realtime import GrokVoice
            return GrokVoice
        from .phonic import Phonic
        return Phonic


PROVIDERS = {p.name: p for p in (
    Provider("gemini", "Gemini Live", "GEMINI_API_KEY", "gemini-3.8-live", "Charon", takes_language=False),
    Provider("openai", "OpenAI Realtime", "OPENAI_API_KEY", "gpt-realtime-2.1", "marin"),
    Provider("grok", "Grok Voice", "XAI_API_KEY", "grok-voice-latest", None),
    Provider("phonic", "Phonic", "PHONIC_API_KEY", "phonic-1", None),
)}


class VoiceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceChoice:
    provider: Provider
    api_key: str
    model: str
    voice: str | None
    language: str | None = None

    def factory(self) -> Callable[[], Any]:
        """A new backend per call (what VoiceAgent takes)."""
        cls = self.provider.backend_class()
        kw: dict[str, Any] = {"model": self.model}
        if self.voice:
            kw["voice"] = self.voice
        if self.language and self.provider.takes_language:
            kw["language"] = self.language
        return lambda: cls(self.api_key, **kw)


def resolve_voice(provider: str | None = None, model: str | None = None, voice: str | None = None) -> VoiceChoice:
    """Arguments win over AGENTRING_VOICE_* settings, which win over the provider's defaults.
    The model and voice settings belong to the configured provider: another one passed as an
    argument starts from its own defaults."""
    configured = (secret("AGENTRING_VOICE_PROVIDER") or DEFAULT_PROVIDER).strip().lower()
    name = (provider or configured).strip().lower()
    p = PROVIDERS.get(name)
    if p is None:
        raise VoiceConfigError(f"unknown voice provider {name!r}: use one of {', '.join(PROVIDERS)}")
    key = secret(p.key_env)
    if not key:
        raise VoiceConfigError(f"{p.label} needs {p.key_env} (environment or ~/.agentring/.env)")
    own = name == configured
    return VoiceChoice(p, key, model or (own and secret("AGENTRING_VOICE_MODEL")) or p.default_model,
                       voice or (own and secret("AGENTRING_VOICE_NAME")) or p.default_voice,
                       secret("AGENTRING_VOICE_LANGUAGE"))
