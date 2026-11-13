"""The voice providers agentring ships, by name: what `--provider` and AGENTRING_VOICE_PROVIDER pick.

    choice = resolve_voice("grok")          # or None: AGENTRING_VOICE_PROVIDER, else gemini
    agent = VoiceAgent(choice.factory(), "You are …", tools)

Defaults come from the environment or ~/.agentring/.env, so the CLI and the MCP server
agree: AGENTRING_VOICE_PROVIDER, AGENTRING_VOICE_MODEL, AGENTRING_VOICE_NAME, the
provider's own key (GEMINI_API_KEY, OPENAI_API_KEY, XAI_API_KEY, PHONIC_API_KEY) and
AGENTRING_VOICE_LANGUAGE (e.g. "es": Phonic speaks it before it hears anyone; OpenAI
and Grok use it to transcribe the user; Gemini follows the instructions).

`local` is any OpenAI-Realtime-compatible server you run yourself (e.g. Hugging Face
speech-to-speech in front of a small model on llama.cpp): AGENTRING_VOICE_URL says where
(default ws://127.0.0.1:8765/v1/realtime), and AGENTRING_VOICE_KEY is only needed if the
server asks for one. The model is whatever that server runs; agentring doesn't pick it.

`openrouter` chains three HTTP calls on OpenRouter with OPENROUTER_API_KEY: speech-to-text,
a chat model (AGENTRING_VOICE_MODEL, any model on OpenRouter) and text-to-speech
(AGENTRING_VOICE_NAME is the TTS voice). AGENTRING_OPENROUTER_STT_MODEL and
AGENTRING_OPENROUTER_TTS_MODEL pick the speech models, AGENTRING_OPENROUTER_ZDR=1 routes
every request only to zero-data-retention endpoints, and AGENTRING_OPENROUTER_BASE_URL points
the cascade at another server with the same OpenAI-compatible endpoints.
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
    key_required: bool = True
    default_url: str | None = None  # only for `local`: where its server listens

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
        if self.name == "local":
            from .openai_realtime import LocalRealtime
            return LocalRealtime
        if self.name == "openrouter":
            from .openrouter import OpenRouterVoice
            return OpenRouterVoice
        from .phonic import Phonic
        return Phonic


PROVIDERS = {p.name: p for p in (
    Provider("gemini", "Gemini Live", "GEMINI_API_KEY", "gemini-3.8-live", "Charon", takes_language=False),
    Provider("openai", "OpenAI Realtime", "OPENAI_API_KEY", "gpt-realtime-2.1", "marin"),
    Provider("grok", "Grok Voice", "XAI_API_KEY", "grok-voice-latest", None),
    Provider("phonic", "Phonic", "PHONIC_API_KEY", "phonic-1", None),
    Provider("local", "Local (OpenAI Realtime compatible)", "AGENTRING_VOICE_KEY", "local", None,
             takes_language=False, key_required=False, default_url="ws://127.0.0.1:8765/v1/realtime"),
    Provider("openrouter", "OpenRouter (speech-to-text, model, text-to-speech)", "OPENROUTER_API_KEY",
             "anthropic/claude-haiku-5.5", "voice_478"),
)}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def openrouter_settings() -> dict[str, Any]:
    """The `openrouter` cascade's own settings (environment or ~/.agentring/.env)."""
    kw: dict[str, Any] = {"zdr": _truthy(secret("AGENTRING_OPENROUTER_ZDR"))}
    for env, arg in (("AGENTRING_OPENROUTER_STT_MODEL", "stt_model"), ("AGENTRING_OPENROUTER_TTS_MODEL", "tts_model"),
                     ("AGENTRING_OPENROUTER_BASE_URL", "base_url")):
        if value := secret(env):
            kw[arg] = value
    base = kw.get("base_url", "")
    if base and not base.startswith(("http://", "https://")):
        raise VoiceConfigError(f"AGENTRING_OPENROUTER_BASE_URL must start with http:// or https://, not {base!r}")
    return kw


class VoiceConfigError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceChoice:
    provider: Provider
    api_key: str
    model: str
    voice: str | None
    language: str | None = None
    url: str | None = None  # `local` only

    def factory(self) -> Callable[[], Any]:
        """A new backend per call (what VoiceAgent takes)."""
        cls = self.provider.backend_class()
        kw: dict[str, Any] = {"model": self.model}
        if self.url:
            kw["url"] = self.url
        if self.voice:
            kw["voice"] = self.voice
        if self.language and self.provider.takes_language:
            kw["language"] = self.language
        if self.provider.name == "openrouter":
            kw.update(openrouter_settings())
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
    if not key and p.key_required:
        raise VoiceConfigError(f"{p.label} needs {p.key_env} (environment or ~/.agentring/.env)")
    url = (secret("AGENTRING_VOICE_URL") or p.default_url) if p.default_url else None
    if url and not url.startswith(("ws://", "wss://")):
        raise VoiceConfigError(f"AGENTRING_VOICE_URL must start with ws:// or wss://, not {url!r}")
    own = name == configured
    return VoiceChoice(p, key or "", model or (own and secret("AGENTRING_VOICE_MODEL")) or p.default_model,
                       voice or (own and secret("AGENTRING_VOICE_NAME")) or p.default_voice,
                       secret("AGENTRING_VOICE_LANGUAGE"), url)
