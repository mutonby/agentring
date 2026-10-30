# Voice AI on calls (M3)

When a call is answered, a realtime speech-to-speech model talks to the user, can
use your tools while the call goes on, and hangs up by itself when the user says
goodbye. It works both ways: the agent calling the phone, and the phone calling
the agent ("Call agent" in the app).

Install the voice extra and give it the key of a voice provider (environment or
`~/.agentring/.env`). Gemini Live is the default (it has a free tier); OpenAI Realtime,
Grok Voice and Phonic work the same way ([providers](#providers)):

```bash
uv tool install './daemon[voice]'
echo GEMINI_API_KEY=... >> ~/.agentring/.env
# or, e.g. Grok for every call (CLI and MCP):
echo XAI_API_KEY=... >> ~/.agentring/.env
echo AGENTRING_VOICE_PROVIDER=grok >> ~/.agentring/.env
```

## Without code

```bash
# Call the phone; the AI explains why and answers questions with your MCP tools.
agentring call "The nightly backup failed" --ai \
  --instructions @prompt.md --mcp http://127.0.0.1:8787/mcp --tools status,logs

# Answer calls from the phone with the AI.
agentring serve --ai --instructions @prompt.md --mcp http://127.0.0.1:8787/mcp
```

- `--instructions`: the system prompt (inline or `@file`). Default: a short, generic phone persona.
- `--mcp`: any MCP server over Streamable HTTP; repeat for several. `--tools` keeps only
  the listed tools: over the phone, expose only what the caller should be able to do.
- `--provider`: `gemini` (default), `openai`, `grok` or `phonic`. Default: `AGENTRING_VOICE_PROVIDER`.
- `--model` / `--voice`: the provider's model and voice (defaults in [providers](#providers));
  `AGENTRING_VOICE_MODEL` / `AGENTRING_VOICE_NAME` set them for the configured provider.
- `--agents`: an [`agents.yaml`](agents.md#agentsyaml) with your agents (the AI consults them with
  `consult_agent`) and MCP servers. Default `~/.agentring/agents.yaml` if it exists; `none` skips it.
- `call --ai --json` prints the whole result (transcript, tool calls, duration).

## As a library

```python
from agentring.relay_client import RelayClient
from agentring.voice import CallInfo, GeminiLive, Prompts, Tool, VoiceAgent  # or OpenAIRealtime, GrokVoice, Phonic

def status(service: str) -> str:
    return "up"

agent = VoiceAgent(
    lambda: GeminiLive(api_key, voice="Charon"),
    instructions="You are the on-call assistant…",   # or a (sync/async) function of CallInfo
    tools=[Tool("status", "Is a service up?", status,
                {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]})],
)
client = RelayClient(relay_url, api_key)
created = client.create_call("The API is down", priority="urgent")
result = await agent.run(CallInfo(created["call"]["call_id"], "The API is down"), created["join"], relay=client)
print(result.end_reason, result.transcript, result.tools)
```

Incoming calls: on each relay event (`agentring.sync.run_ws`), `accept_incoming(client, event)`
returns `(CallInfo, join)` for a call from the phone, ready for `agent.run`.

What you can plug in:

| Option | What it does |
|---|---|
| `tools` | `Tool(name, description, handler, parameters)`; sync handlers run in a thread, async ones in the loop. `mcp_tools(url, allow=[…])` turns an MCP server into tools; `Consultations(load_agents(path).agents).tool()` is `consult_agent` ([agents.md](agents.md)). A tool can find its call with `current_session()` and answer later with `session.notify(text, respond=True)`. |
| `instructions` | Text, or a function of `CallInfo` (e.g. to add fresh context when the call starts). |
| `prompts` | `Prompts(...)`: the notes the session gives the model when someone answers, joins or leaves, and the `end_call` description. Change them to change wording or language. |
| `name_of` | LiveKit identity (`device_<id>`) → the person's name, so the model greets them by name. |
| `before_tool` | `(session, call) → None \| dict \| str`: run, run with other args, or skip with that answer. The session exposes `transcript`, `partial`, `participants` and `last_user_speech_at` (e.g. "only act if the user asked in the last 45 s"). |
| `end_call_tool`, `tool_timeout_s`, `ring_timeout_s`, `max_duration_s`, `empty_room_grace_s` | Limits. A tool slower than its timeout keeps running; the model is told so. |

Behaviour: the agent joins the room while the phone rings and opens the model
session at the same time, so it greets as soon as someone answers. In group
calls everyone's audio is mixed into one input and the model is told who joins
or leaves. Talking over the model interrupts it. Tools run in their own task, so
the call keeps listening while a slow tool works. The call ends when the model
calls `end_call` (after its goodbye), everyone leaves (10 s grace for a dropped
connection), the relay says it is over, or the model's session limit is near
(Gemini: 15 min; OpenAI: 60 min).

## Providers

| `--provider` | Model (default) | Voice (default) | Key | Class |
|---|---|---|---|---|
| `gemini` | Gemini Live (`gemini-3.8-live`) | `Charon` | `GEMINI_API_KEY` | `GeminiLive` (voice/gemini.py) |
| `openai` | OpenAI Realtime (`gpt-realtime-2.1`) | `marin` | `OPENAI_API_KEY` | `OpenAIRealtime` (voice/openai_realtime.py) |
| `grok` | xAI Grok Voice (`grok-voice-latest`) | xAI's default | `XAI_API_KEY` | `GrokVoice` (voice/openai_realtime.py) |
| `phonic` | Phonic (`phonic-1`) | Phonic's default | `PHONIC_API_KEY` | `Phonic` (voice/phonic.py) |

The CLI (`call --ai`, `serve --ai`) and the MCP `call` tool resolve the provider the
same way: `--provider`/`--model`/`--voice` if given, else `AGENTRING_VOICE_PROVIDER`,
`AGENTRING_VOICE_MODEL`, `AGENTRING_VOICE_NAME` (environment or `~/.agentring/.env`),
else the defaults above. In code: `resolve_voice("grok").factory()` is what `VoiceAgent`
takes. `AGENTRING_VOICE_LANGUAGE` (e.g. `es`) is the language Phonic speaks before it has
heard anyone (otherwise English) and a hint for OpenAI's and Grok's transcription of the
user; Gemini follows the instructions.

How they compare, measured on 2026-10-30 with the same four Spanish prompts streamed in
real time, 3 sessions each (chat, a quick tool, two chained tools, a 3 s tool). Time is
from the end of the user's speech to the model's first audio; "right tools" means it
picked and chained the expected tools:

| | Phonic-1 | Grok Voice | Gemini 3.8 Live |
|---|---|---|---|
| First audio, chat | 1.18 s | 1.26 s | 1.60 s |
| First audio, with tools | 1.20–1.52 s | 1.30–1.59 s | 2.00–5.30 s |
| Right tools | 8/9 | 9/9 | 7/9 |

Phonic and Grok say "let me check" before a tool and then answer; Gemini stays quiet
until the tool returns. Phonic talks the longest, Grok is the most concise. OpenAI
Realtime was not part of that run.

Pricing: Gemini Live has a free tier (rate-limited; Google may use free-tier data to
improve its products) and a paid tier of about $0.005/min of audio in and $0.018/min out
(https://ai.google.dev/gemini-api/docs/pricing). Phonic starts at $0.07/min
(https://phonic.ai). OpenAI and xAI bill per use; see their pricing pages.

## Backends

`RealtimeBackend` (voice/backend.py) is the plug point: a class that connects with
instructions + tools, sends audio/text/tool results and yields a few plain events.
Each provider above is one class, spoken to over its WebSocket API with `websockets`
(no provider SDKs):

- **OpenAI Realtime** (GA protocol): PCM16 at 24 kHz both ways, server VAD, input
  transcription. Function calls are collected until `response.done` and answered with
  one `response.create`.
- **Grok Voice** speaks the same protocol (`GrokVoice` subclasses `OpenAIRealtime`) with
  a flatter session (voice and turn detection at the top) and `grok-transcribe`.
- **Phonic** has its own protocol (https://docs.phonic.ai/asyncapi.yaml). Its inline tools
  need OpenAI strict schemas, so `strict_schema` closes every object and makes optional
  arguments nullable (the nulls are dropped before your handler runs). It streams audio
  all the time, silence included, so only what it sends while the assistant speaks
  reaches the call.

A local OpenAI-Realtime-compatible server can use `OpenAIRealtime(key, url="ws://…")`.

## Testing

`tests/test_voice.py` and `tests/test_voice_providers.py` cover the protocols (against
fake servers), tools, MCP and session logic without a phone. `tests/test_voice_live.py`
runs whole calls (the AI calls a simulated phone, and the phone calls
`agentring serve --ai`) against a real relay, LiveKit and a real model, Gemini unless
`AGENTRING_TEST_PROVIDER` says otherwise:

```bash
docker run -d --name lk -p 7880:7880 -p 7881:7881 -p 50000-50100:50000-50100/udp \
  -e "LIVEKIT_KEYS=devkey: $(openssl rand -hex 24 | tee /tmp/lk)" \
  -e 'LIVEKIT_CONFIG=rtc: {port_range_start: 50000, port_range_end: 50100, tcp_port: 7881}' \
  livekit/livekit-server:v1.13.10 --node-ip "$(ipconfig getifaddr en0)" --bind 0.0.0.0
AGENTRING_TEST_LIVEKIT_URL=ws://127.0.0.1:7880 AGENTRING_TEST_LIVEKIT_SECRET=$(cat /tmp/lk) \
GEMINI_API_KEY=... uv run --extra voice pytest tests/test_voice_live.py -s
# another provider: AGENTRING_TEST_PROVIDER=grok XAI_API_KEY=... (same for openai, phonic)
```
