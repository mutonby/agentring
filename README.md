# agentring

**Your AI agent can call you.** When it needs a decision, hits a wall or finishes a
long job, your phone rings like a real call, even locked and in Doze. A voice AI tells
you what's going on, you talk it through, and the agent gets the transcript and carries
on. You can also call your agents back from the app and ask them things by voice.

Self-hosted and open source. No phone numbers, no Twilio, no per-minute fees: calls go
over VoIP from your own server to your own phone.

[![CI](https://github.com/mutonby/agentring/actions/workflows/ci.yml/badge.svg)](https://github.com/mutonby/agentring/actions/workflows/ci.yml)
[![Android](https://github.com/mutonby/agentring/actions/workflows/android.yml/badge.svg)](https://github.com/mutonby/agentring/actions/workflows/android.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

https://github.com/user-attachments/assets/4d925f22-727f-4d66-8665-c392cc3d1eb3

*Sound on. A real run with Claude Code, a Pixel 6 and the voice AI: a migration fails, Claude Code
calls the locked phone, you decide, it carries on. The phone is a screen recording with the call
audio (the ringtone was added), the terminal is redrawn from Claude Code's output, and only the waits were cut. Silent
version: [demo.gif](docs/media/demo.gif).*

> **Pre-release.** Messages, calls on a locked phone, the voice AI, calling your agent
> from the app and the MCP server all work. So far it has been tested on one phone
> (Pixel 6, Android 16). Android only for now.

## What it does

- **Your agent reaches you.** It can send you a message, wait for your reply or call
  you (`send_message`, `get_replies`, `call`). On a call, a voice AI explains why the
  agent is calling, answers your questions and hands the transcript back.
- **You reach your agents.** Tap "Call agent" in the app. The voice AI can ask your own
  agents for help mid-call. If one is slow, the call goes on and its answer arrives
  when it's ready, or as a message if you've already hung up.
- **Nothing gets lost.** A missed call turns into a message you can answer from the
  notification.

Things people use it for: *"run the migration and call me if anything fails"*, *"ask
me before you deploy"*, *"what did CI say?"* while driving, a daily call with your
assistant about your inbox.

## Works with your agent

agentring doesn't care which harness you use: anything that speaks MCP can reach you,
and anything you can run as a command can be consulted from a call.

| | Reaches you | You consult it from a call |
|---|---|---|
| Claude Code | MCP | `claude -p` |
| Codex | MCP | `codex exec` |
| opencode | MCP | `opencode run` |
| OpenClaw | MCP | `openclaw agent` |
| Hermes | MCP | `hermes -z` |
| n8n, scripts, anything with HTTP | REST API, CLI | any command, or a small script for an HTTP agent |

Copy-paste setup for each: [docs/agents.md](docs/agents.md#recipes-by-harness).

## How it works

```
 your agent ──MCP/CLI──▶ daemon ──WSS (outbound only)──▶ relay ──FCM push──▶ Android app
     ▲                     │                      (your server)                   │
     └── agents.yaml ◀── voice AI (realtime) ◀──────── LiveKit (call audio) ───────┘
```

- **relay** (TypeScript): pairing, messages, call state, push, LiveKit tokens. Runs on
  your server behind Caddy.
- **LiveKit**: the call audio, self-hosted in the same compose file.
- **daemon** (Python): the `agentring` CLI, the MCP server and the voice AI. Runs next
  to your agents, behind NAT is fine.
- **Android app** (Kotlin): QR pairing, the native call screen, messages.

The voice AI is the voice, your agent is the brain: a realtime speech model handles
the conversation and hands real work to your agents. Pick the one you like:

| Voice AI | `--provider` | Key |
|---|---|---|
| [Gemini Live](https://ai.google.dev/gemini-api/docs/live) (default, free tier) | `gemini` | `GEMINI_API_KEY` |
| [OpenAI Realtime](https://developers.openai.com/api/docs/guides/realtime-conversations) | `openai` | `OPENAI_API_KEY` |
| [Grok Voice](https://docs.x.ai/docs/guides/voice/agent) (xAI) | `grok` | `XAI_API_KEY` |
| [Phonic](https://docs.phonic.ai) | `phonic` | `PHONIC_API_KEY` |

Set `AGENTRING_VOICE_PROVIDER` in `~/.agentring/.env` to use one for every call, from the
CLI and from MCP. How they compare (latency, tool use) and how to add another one:
[docs/voice.md](docs/voice.md#providers).

More: [architecture](docs/architecture.md) · [protocol](docs/protocol.md) ·
[voice AI](docs/voice.md)

## Quickstart

You need:
- somewhere to run the relay: a Linux server with Docker and a domain name, **or** your own
  computer with Docker (the phone on the same Wi-Fi, or anywhere over Tailscale),
- a free Firebase project, which is what makes a locked phone ring ([docs/firebase.md](docs/firebase.md)),
- a key for the voice AI: Gemini ([free tier](https://aistudio.google.com/apikey)), or OpenAI,
  xAI (Grok) or Phonic ([providers](docs/voice.md#providers)),
- an Android phone.

```bash
# 1. The relay, on a server with a domain (ports 80, 443, UDP 50000-50100 and TCP 7881 open)
cd deploy && cp .env.example .env && $EDITOR .env && docker compose up -d --build
#    ... or on your own computer, at its LAN or Tailscale IP
cd deploy && cp .env.local.example .env.local && $EDITOR .env.local \
  && docker compose -f docker-compose.local.yml --env-file .env.local up -d --build
docker compose logs relay | grep -A2 "API key"   # add the same -f/--env-file for the local one

# 2. The computer where your agent runs
uv tool install './daemon[voice]'
agentring init --relay https://relay.example.com --api-key ak_...
agentring pair                         # scan the QR code with the app
agentring msg "hello"                  # the phone gets a notification
echo GEMINI_API_KEY=... >> ~/.agentring/.env  # or OPENAI_API_KEY / XAI_API_KEY / PHONIC_API_KEY
agentring call "Testing agentring" --ai       # + --provider openai|grok|phonic

# 3. Your agent (Claude Code here; others: docs/agents.md)
claude mcp add agentring -- agentring mcp
```

Get the app from [Releases](https://github.com/mutonby/agentring/releases). The full
tutorial, step by step for both options and with a troubleshooting table:
**[docs/setup.md](docs/setup.md)**. If the phone doesn't ring when locked, it's usually the
vendor's battery saver: [docs/troubleshooting-oem.md](docs/troubleshooting-oem.md).

## Privacy and security

Everything runs on your own servers and accounts. Messages and call metadata are stored
on your relay. Push notifications go through your own Firebase project; they are
signed, not encrypted, so Google's FCM can read pushed text. Call audio goes through
your LiveKit server and, with the voice AI, to the provider you chose (Google, OpenAI,
xAI or Phonic); on Google's free tier, Google may use that data to improve its products.

Over the phone, the voice AI acts on what it hears, so give it only the tools and
agents you'd trust someone holding your phone with. Details and the threat model:
[SECURITY.md](SECURITY.md).

## Limitations

- Android only (no iOS yet).
- One user per relay: no accounts.
- The voice AI needs a cloud realtime model (Gemini, OpenAI, Grok or Phonic); others,
  local ones included, plug in through [`RealtimeBackend`](docs/voice.md#backends).

## Development

```bash
cd relay && npm ci && npm test                         # relay
cd daemon && uv run --extra voice pytest               # daemon (+ e2e against the relay build)
cd app-android && ./gradlew :core:test :app:assembleDebug
```

CI builds the APK and runs an end-to-end test on an Android emulator against a real
relay. See [CONTRIBUTING.md](CONTRIBUTING.md) and [docs/release.md](docs/release.md).

## License

[Apache-2.0](LICENSE)
