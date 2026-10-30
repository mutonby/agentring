# OpenClaw + agentring

Two things to set up: your OpenClaw assistant can reach you (MCP server + skill), and
you can reach it from a call (an agent in `agents.yaml`). Recipes for other harnesses:
[docs/agents.md](../../docs/agents.md#recipes-by-harness).

## OpenClaw reaches you

```bash
agentring init --relay https://relay.example.com --api-key ak_...
agentring pair                                   # scan the QR with the app
openclaw mcp set agentring '{"command":"agentring","args":["mcp"]}'
cp -r ../../skills/agentring ~/.openclaw/skills/  # when to message and when to call
```

Calls need the voice extra and a Gemini API key (free tier available at
https://aistudio.google.com/apikey), or the key of another voice provider (OpenAI, Grok,
Phonic: [docs/voice.md](../../docs/voice.md#providers)):

```bash
uv tool install 'agentring[voice]'
echo GEMINI_API_KEY=... >> ~/.agentring/.env
```

Then, for example: *"Check my inbox every hour; if something is urgent, call me."*

## You reach OpenClaw by phone

`~/.agentring/agents.yaml`:

```yaml
agents:
  openclaw:
    description: My OpenClaw assistant. Messages, calendar, reminders, home.
    command: [sh, -c, 'exec openclaw agent --agent main --session-id "agentring-${AGENTRING_CALL_ID:-test}" --message="$1"', sh, "{task}"]
    timeout_s: 600
```

`openclaw agent` runs one turn through your gateway, so keep it running
(`openclaw gateway`). The `sh -c` script is fixed text: the task arrives as `$1` and
is never parsed by the shell. The call id gives each call its own session, so a
follow-up question keeps the context. Change `main` to use another of your agents.

```bash
agentring agents ask openclaw "What's on my calendar tomorrow?"   # check it works
agentring serve --ai                                               # answers "Call agent" from the app
```

On the call: *"Ask OpenClaw to remind me to call the bank at five."* The voice AI
asks it, tells you it's on it, and reads you the answer when it comes. If you've
already hung up, the answer arrives as a message.

The voice AI acts on what it hears: give the phone only the agents and channels you'd
accept from someone holding it.
