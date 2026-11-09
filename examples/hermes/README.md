# Hermes + agentring

Two things to set up: your Hermes agent can reach you (MCP server + skill), and you can
reach it from a call (an agent in `agents.yaml`). Recipes for other harnesses:
[docs/agents.md](../../docs/agents.md#recipes-by-harness).

## Hermes reaches you

The quickest way: install the skill and ask Hermes to "set up agentring"; it walks you
through the rest ([setup guide](../../skills/agentring/references/setup.md)).

```bash
hermes skills install mutonby/agentring/skills/agentring
```

Or by hand:

```bash
uv tool install 'agentring[voice] @ git+https://github.com/mutonby/agentring#subdirectory=daemon'
agentring init --relay https://relay.example.com --api-key ak_...
agentring pair                                   # scan the QR with the app
agentring connect hermes                         # MCP server, skill and agents.yaml
echo GEMINI_API_KEY=... >> ~/.agentring/.env     # the voice AI on calls
```

`agentring connect hermes` adds `agentring mcp` to `mcp_servers` in
`~/.hermes/config.yaml` (`$HERMES_HOME`) with `timeout: 1800` (Hermes' default is
300 s, shorter than a long call), installs the skill in `~/.hermes/skills/agentring` and
adds Hermes to `~/.agentring/agents.yaml`. Then run `/reload-mcp` or start a new session;
`hermes mcp test agentring` checks the server.

With agentring cloud, nothing to run but Hermes:

```bash
AGENTRING_API_KEY=ak_... agentring connect hermes --url https://agentring.io/mcp
```

The key goes to `~/.hermes/.env`; the config only says `Bearer ${AGENTRING_API_KEY}`.

Calls need a voice provider key (Gemini has a free tier:
https://aistudio.google.com/apikey; others: [docs/voice.md](../../docs/voice.md#providers)).
With agentring cloud the voice AI runs on the server.

Then, for example: *"Every morning, read my inbox; if something can't wait, call me."*

## You reach Hermes by phone

`agentring connect hermes` added this to `~/.agentring/agents.yaml`:

```yaml
agents:
  hermes:
    description: My Hermes agent. Email, calendar, research.
    command: [/path/to/hermes, -z, "Task: {task}"]
    timeout_s: 600
```

`-z` runs one turn and prints only the final answer. Don't add `--yolo` for the phone.

```bash
agentring agents ask hermes "What's on my calendar tomorrow?"   # check it works
agentring serve --ai                                             # answers "Call agent" from the app
```

On the call: *"Ask Hermes to find the cheapest flight to Lisbon on Friday."* The voice
AI asks it, tells you it's on it, and reads you the answer when it comes. If you've
already hung up, the answer arrives as a message.

The voice AI acts on what it hears: give the phone only the agents and channels you'd
accept from someone holding it.
