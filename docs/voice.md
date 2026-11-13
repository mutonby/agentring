# Voice AI on calls (M3)

When a call is answered, a realtime speech-to-speech model talks to the user, can
use your tools while the call goes on, and hangs up by itself when the user says
goodbye. It works both ways: the agent calling the phone, and the phone calling
the agent ("Call agent" in the app).

Install the voice extra and give it the key of a voice provider (environment or
`~/.agentring/.env`). Gemini Live is the default (it has a free tier); OpenAI Realtime,
Grok Voice and Phonic work the same way, and `local` uses a voice server on your own
machine with no key ([providers](#providers), [local](#local-voice-experimental)):

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
- `--provider`: `gemini` (default), `openai`, `grok`, `phonic` or `local`. Default: `AGENTRING_VOICE_PROVIDER`.
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
| `tools` | `Tool(name, description, handler, parameters)`; sync handlers run in a thread, async ones in the loop. `mcp_tools(url, allow=[…])` turns an MCP server into tools; `Consultations(load_agents(path).agents).tool()` is `consult_agent` ([agents.md](agents.md)). A tool can find its call with `current_session()` and answer later with `session.notify(text, respond=True)`; pass `trusted=False` if that text is another program's output (see below). |
| `instructions` | Text, or a function of `CallInfo` (e.g. to add fresh context when the call starts). |
| `prompts` | `Prompts(...)`: the notes the session gives the model when someone answers, joins or leaves, the `end_call` description, and `before_tools`, the line added to the instructions when there are tools ("say a few words before you call a tool, never answer from memory what a tool should tell you"; `""` leaves the instructions alone). Change them to change wording or language. |
| `name_of` | LiveKit identity (`device_<id>`) → the person's name, so the model greets them by name. |
| `before_tool` | `(session, call) → None \| dict \| str`: run, run with other args, or skip with that answer. The session exposes `transcript`, `partial`, `participants` and `last_user_speech_at` (e.g. "only act if the user asked in the last 45 s"). `build_agent(..., before_tool=…)` takes it too. |
| `end_call_tool`, `tool_timeout_s`, `ring_timeout_s`, `max_duration_s`, `empty_room_grace_s` | Limits. A tool slower than its timeout keeps running; the model is told so. |

Behaviour: the agent joins the room while the phone rings and opens the model
session at the same time, so it greets as soon as someone answers. In group
calls everyone's audio is mixed into one input and the model is told who joins
or leaves. Talking over the model interrupts it. Tools run in their own task, so
the call keeps listening while a slow tool works. The call ends when the model
calls `end_call` (after its goodbye), everyone leaves (10 s grace for a dropped
connection), the relay says it is over, or the model's session limit is near
(Gemini: 15 min; OpenAI: 60 min).

**Text the voice AI should not obey.** Two things can reach the model from other programs:

- The MCP `call` tool's `context` comes from the calling agent, which may be passing on
  text it read somewhere. So on those calls `before_tool` defaults to `after_user_speech`
  (`agentring.voice`): every tool is refused, with a note telling the model to ask,
  until someone on the call has spoken, going by the transcript. Hanging up is never
  held back. `AgentringTools(..., before_tool=…)` replaces it. `call --ai` and
  `serve --ai` have no gate: their instructions are your own.
- A late `consult_agent` answer is an agent's output, so it is a note with `trusted=False`.
  `RealtimeBackend.send_text(text, *, respond, trusted=True)` gets that flag. Gemini,
  OpenAI and Grok send every note with the `user` role and ignore it. Phonic only takes
  text as a system message, so it sends such a note fenced as data, with a random tag and
  a line telling the model not to obey it. The fence is a label, not a guarantee. A custom
  backend without the `trusted` parameter keeps working and gets these notes as text.

## Providers

| `--provider` | Model (default) | Voice (default) | Key | Class |
|---|---|---|---|---|
| `gemini` | Gemini Live (`gemini-3.8-live`) | `Charon` | `GEMINI_API_KEY` | `GeminiLive` (voice/gemini.py) |
| `openai` | OpenAI Realtime (`gpt-realtime-2.1`) | `marin` | `OPENAI_API_KEY` | `OpenAIRealtime` (voice/openai_realtime.py) |
| `grok` | xAI Grok Voice (`grok-voice-latest`) | xAI's default | `XAI_API_KEY` | `GrokVoice` (voice/openai_realtime.py) |
| `phonic` | Phonic (`phonic-1`) | Phonic's default | `PHONIC_API_KEY` | `Phonic` (voice/phonic.py) |
| `local` | whatever your server runs | your server's | none (`AGENTRING_VOICE_KEY` if it asks for one) | `LocalRealtime` (voice/openai_realtime.py) |
| `openrouter` | any chat model on OpenRouter (`anthropic/claude-haiku-5.5`), with speech-to-text and text-to-speech models in front and behind | `voice_478` (Voxtral 3) | `OPENROUTER_API_KEY` | `OpenRouterVoice` (voice/openrouter.py) |

The CLI (`call --ai`, `serve --ai`) and the MCP `call` tool resolve the provider the
same way: `--provider`/`--model`/`--voice` if given, else `AGENTRING_VOICE_PROVIDER`,
`AGENTRING_VOICE_MODEL`, `AGENTRING_VOICE_NAME` (environment or `~/.agentring/.env`),
else the defaults above. In code: `resolve_voice("grok").factory()` is what `VoiceAgent`
takes. `AGENTRING_VOICE_LANGUAGE` (e.g. `es`) is the language Phonic speaks before it has
heard anyone (otherwise English) and a hint for OpenAI's and Grok's transcription of the
user; Gemini follows the instructions.

### OpenRouter (speech-to-text → any model → text-to-speech)

OpenRouter has no realtime speech-to-speech API, but it serves the three pieces over HTTP
with one key, so `openrouter` chains them: the caller's audio is cut into utterances by a
small voice activity detector, transcribed (`/audio/transcriptions`), answered by any chat
model with your tools (`/chat/completions`, streamed) and spoken sentence by sentence
(`/audio/speech`, streamed PCM) while the model is still writing. Talking over the agent
interrupts it. That lets you pick the model that thinks on the call (and pay per token, not
per audio minute), at the cost of some latency: about 2.5-3 s to the first word with
`anthropic/claude-haiku-5.5`, against 1.2-1.6 s for the realtime models below.

```bash
OPENROUTER_API_KEY=sk-or-...
AGENTRING_VOICE_PROVIDER=openrouter
AGENTRING_VOICE_MODEL=anthropic/claude-haiku-5.5           # any chat model with tool calling
AGENTRING_VOICE_NAME=voice_483                             # a TTS voice (voice_483: Voxtral 3, Spanish)
AGENTRING_VOICE_LANGUAGE=es                                # hint for the transcription
AGENTRING_OPENROUTER_STT_MODEL=mistralai/voxtral-mini-transcribe-3-0   # default
AGENTRING_OPENROUTER_TTS_MODEL=mistralai/voxtral-mini-tts-3-0          # default
AGENTRING_OPENROUTER_ZDR=1            # optional: only zero-data-retention endpoints, for all three
AGENTRING_OPENROUTER_BASE_URL=...     # optional: another server with the same OpenAI-compatible endpoints
```

With `AGENTRING_OPENROUTER_ZDR=1` every request carries `provider: {zdr: true,
data_collection: "deny"}`, so OpenRouter only routes to endpoints that keep nothing, and
fails rather than fall back to one that does. Measured with `agentring voice bench
--provider openrouter --lang es --runs 2` on 2026-11-13 (Haiku 5.5, Voxtral STT and TTS):
first audio 2.5-3.3 s, right tools 6-7/8 (it sometimes answers the "ask an agent" prompt itself
instead of calling `consult_agent`), spoke before a tool 4-6/6.

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
Realtime was not part of that run. Write-up with charts: https://agentring.io/blog/realtime-voice-models.

To compare providers yourself, on your machine and with your network, run the same test:

```bash
agentring voice bench --provider grok --lang es      # or gemini, openai, phonic, openrouter, local
agentring voice bench --provider local --runs 5 --out ./bench-local   # keeps each session's audio
```

It speaks the four prompts with the system's text-to-speech (`say` on macOS, `espeak-ng`
elsewhere; `--prompts DIR` takes your own `chat.wav`, `time.wav`, `chain.wav`, `consult.wav`),
streams them in real time with the same demo tools, and prints per prompt the median time to
the first word, how often the right tools were called and how often the model spoke before a
tool answered. It uses the provider's real API: about 12 short sessions with `--runs 3`. `--instructions`
swaps the system prompt, e.g. to A/B a change.

What that showed for the `before_tools` line (2026-11-05, `--lang es --runs 3`, macOS voice):

| | First word, slow tool | Spoke before a tool answered | Right tools |
|---|---|---|---|
| Gemini 3.8 Live, without the line | 5.13 s | 0/9 | 10/12 |
| Gemini 3.8 Live, with it | **1.45 s** | **9/9** | 11/12 |
| Grok Voice, with it (it already talked before tools) | 1.17 s | 9/9 | 12/12 |
| Local, Ministral 3 3B, without | 5.55 s | 0/9 | 10/12 |
| Local, Ministral 3 3B, with it | 2.46 s | 2/9 | 10/12 |

Gemini stopped going quiet while a tool works; the small local model only sometimes follows it.

Pricing: Gemini Live has a free tier (rate-limited; Google may use free-tier data to
improve its products) and a paid tier of about $0.005/min of audio in and $0.018/min out
(https://ai.google.dev/gemini-api/docs/pricing). Phonic starts at $0.07/min
(https://phonic.ai). OpenAI and xAI bill per use; see their pricing pages.

## Local voice (experimental)

`--provider local` talks to an OpenAI-Realtime-compatible server you run yourself: no key,
no per-minute bill, and the call audio stays on your machine. agentring doesn't ship or pick
a model; it connects to `AGENTRING_VOICE_URL` (default `ws://127.0.0.1:8765/v1/realtime`) and
the server decides who listens, thinks and speaks. Use `wss://` (or a private network such as
Tailscale) if the server is on another machine: the URL carries the call audio.

What worked best in our tests on an M1 Pro (32 GB) is [Hugging Face
speech-to-speech](https://github.com/huggingface/speech-to-speech) 1.0.0 (Parakeet for
speech-to-text, Qwen3-TTS for the voice) with the language model served by llama.cpp:

```bash
# 1. The language model: Ministral 3 3B, 4-bit (about 2 GB), on llama.cpp
brew install llama.cpp   # or a release from github.com/ggml-org/llama.cpp
curl -LO https://huggingface.co/mistralai/Ministral-3-3B-Instruct-2512-GGUF/resolve/main/Ministral-3-3B-Instruct-2512-Q4_K_M.gguf
llama-server -m Ministral-3-3B-Instruct-2512-Q4_K_M.gguf --jinja -ngl 99 -c 16384 --port 8082

# 2. The voice server in front of it (another terminal; downloads ~2.5 GB of models the first time)
uv venv -p 3.12 s2s && uv pip install -p s2s/bin/python speech-to-speech==1.0.0
s2s/bin/speech-to-speech serve --mac-optimal-settings --llm_backend chat-completions \
  --responses_api_base_url http://127.0.0.1:8082/v1 --responses_api_api_key none --model_name ministral \
  --parakeet_tdt_language es --qwen3_tts_language Spanish --port 8765   # or en / English

# 3. agentring
agentring voice bench --provider local --lang es   # check it before a real call
agentring call "Testing the local voice" --ai --provider local
```

Any model `llama-server` can run works: change the `.gguf`. What we measured
(write-up: https://agentring.io/blog/local-voice-models):

| Language model (4-bit, llama-server) | Tool prompts right, text only (12) | Full voice chain on the M1 Pro: chat / quick tool | Notes |
|---|---|---|---|
| **Ministral 3 3B** | 12/12 | 2.42 s / 1.76 s | 7/9 right tools by voice; went quiet during a 3 s tool |
| Qwen3-4B-Instruct-2507 | 12/12 | 2.39 s / 1.65 s | 7/9; twice answered without calling the slow tool |
| Qwen3.5 2B | 12/12 | not measured | |
| Qwen3.5 4B | 8/12 | not measured | |
| LFM2.5 1.2B | 6/12 | not measured | |
| Gemma 4 E4B (hears the audio itself, no speech-to-text) | not run this way | 1.28 s / 1.31 s | 0/9 right tools by voice: it only uses tools when it thinks, and thinking takes 2–9 s |

For comparison, Grok Voice answered in about 1.3 s with 9/9 right tools. Known rough edges:
the speech-to-text mishears now and then, the language model and the voice share the Mac's
GPU (first audio 0.3–2 s depending on load), and the server takes one call at a time (6–7 GB of
memory). HF speech-to-speech's own Mac default runs the model on MLX, which was 3–4 times
slower in our test than the same model on llama.cpp.

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

A local OpenAI-Realtime-compatible server is `LocalRealtime(url="ws://…")` (what `--provider local`
uses): the same protocol, no key, no voice or transcription model of its own.

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
