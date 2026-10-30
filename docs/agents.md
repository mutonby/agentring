# Connecting agents

There are two directions, and you can use both:

1. **Your agent reaches you.** Claude Code, OpenClaw, n8n or a script messages your
   phone, waits for your reply, or calls you. A voice AI talks to you for the
   agent and hands it the transcript. Use the [MCP server](#mcp-server-agentring-mcp),
   the CLI or the relay's REST API.
2. **You reach your agents by phone.** On a call (one the agent placed, or one you
   made with "Call agent" in the app), the voice AI can ask your agents for help.
   Each agent in [`agents.yaml`](#agentsyaml) is a tool for it, through
   `consult_agent`, and so are your MCP servers.

## MCP server (`agentring mcp`)

`agentring mcp` is an MCP server over stdio. Add it to any MCP client:

```bash
claude mcp add agentring -- agentring mcp          # Claude Code
```

```json
{ "mcpServers": { "agentring": { "command": "agentring", "args": ["mcp"] } } }
```

The second form is the usual `mcpServers` config that most clients read. It uses
the daemon's config (`agentring init`), so run `agentring init` and
`agentring pair` first. Other harnesses: see [recipes](#recipes-by-harness).

`agentring mcp --agent-name "Claude Code"` sets the caller name the phone shows (useful
when several agents share one relay), and `--device dev_…` (repeatable) rings only those
phones instead of every paired one.

`call` blocks until the call ends, and `get_replies` up to `wait_s`: raise the
client's MCP tool timeout if it has a short one (Codex: 60 s, opencode: 30 s).

| Tool | What it does |
|---|---|
| `send_message(text, reply_to?)` | Sends a message to the phone (a notification you can reply to inline). Returns `msg_id`. |
| `get_replies(wait_s?)` | Your new replies, each one returned once. `wait_s` waits up to that long for one (max 3600). |
| `call(reason, context?, urgent?)` | The phone rings. When you answer, the voice AI says why it's calling, talks with you, and hangs up when you're done. Returns the transcript, how long the call lasted, why it ended and which tools were used. Blocks until the call ends. Needs the `[voice]` extra and the voice provider's key (`GEMINI_API_KEY` by default; `AGENTRING_VOICE_PROVIDER` picks another, see [voice.md](voice.md#providers)). |
| `status()` | Whether the relay is reachable and which phones are paired. |

`context` is everything the voice AI knows about the agent's work: put in it what
to tell you, what to ask and what not to decide. If `~/.agentring/agents.yaml` exists,
the voice AI on these calls can also consult your agents.

The server is a small JSON-RPC loop with no MCP SDK and no extra dependencies
(see [architecture](architecture.md#accepted-deviations)). It does not support
Streamable HTTP. HTTP-only clients such as n8n use the REST API (below).

## agents.yaml

`~/.agentring/agents.yaml` (or `--agents PATH`) lists what the voice AI may use
during a call:

```yaml
agents:
  claude:
    description: Claude Code in my web app's repo. Code questions, CI status, deploys.
    command: [claude, -p, --permission-mode, plan, --, "{task}"]
    cwd: ~/code/webapp
    timeout_s: 600          # hard limit; the process is killed after it (default 600)
  ops:
    description: My ops scripts. Disk, backups, service status.
    command: [~/bin/ops-agent]   # no "{task}": the task arrives on stdin
    env: {OPS_PROFILE: prod}

mcp:                         # MCP servers over Streamable HTTP; their tools go straight to the voice AI
  - url: http://127.0.0.1:8787/mcp
    tools: [status, logs]    # allowlist; omit it to expose all of the server's tools
    headers: {Authorization: Bearer ...}
```

- **`command` is an argv list, never a shell string** (a string is rejected). The
  task comes from a voice model, so it is only ever one argument: `{task}` is
  replaced inside the argument that contains it, and if no argument has it, the
  task goes to stdin. The agent's stdout is the answer. A non-zero exit is an
  error, with the end of stderr.
- Don't let the task be read as an option: put `--` before `{task}`, or text before
  it in the same argument (`"Task: {task}"`).
- The agent runs with your environment plus `env` and `AGENTRING_CALL_ID`, in `cwd`.
  The call id lets an agent keep one session per call (see [OpenClaw](#openclaw)).
- **`description` matters**: the voice AI picks the agent by it.
- Over the phone, expose only what the caller should be able to do: use
  allowlists, and agents whose permissions you'd accept from someone holding your
  phone. The phone is paired to your relay, but the voice AI acts on what it hears.

Check the file and try an agent without a call:

```bash
agentring agents                       # lists them; errors if the file is wrong
agentring agents ask claude "Is CI green on main?"
```

Then use it on calls:

```bash
agentring serve --ai                   # answers "Call agent" from the app
agentring call "Deploy needs your OK" --ai
```

### How a consultation goes

`consult_agent(agent, task)` waits up to 20 s. If the agent answers in that time,
the voice AI gets the answer right away. If not, it hears that the agent is working
on it, tells you, and the call carries on. When the answer arrives, the voice AI gets
it as a note and tells you. If the call has already ended, the answer is sent to
your phone as a message. Each agent works on one task at a time; a second request
for a busy agent is refused, so the model asks again later.

From Python, `Consultations(agents, answer_within_s=…, prompts=ConsultPrompts(…))`
builds the tool (`.tool()`) for a `VoiceAgent`. Any tool of your own can do the same
"answer later" trick: `current_session()` returns the call, and
`session.notify(text, respond=True)` speaks up when you are done.

## Recipes by harness

agentring doesn't care which harness you use: anything that speaks MCP can reach
you, and anything you can run as a command can be consulted from a call.

### Your agent reaches you (MCP)

| Harness | Setup |
|---|---|
| Claude Code | `claude mcp add agentring -- agentring mcp` |
| Codex | `codex mcp add agentring -- agentring mcp`, then `tool_timeout_sec = 1800` under `[mcp_servers.agentring]` in `~/.codex/config.toml` |
| opencode | `opencode.json`: `"mcp": {"agentring": {"type": "local", "command": ["agentring", "mcp"], "timeout": 1800000}}` |
| OpenClaw | `openclaw mcp set agentring '{"command":"agentring","args":["mcp"]}'` |
| Hermes | `~/.hermes/config.yaml`: `mcp_servers: {agentring: {command: agentring, args: [mcp], timeout: 1800}}` |
| Anything else | the `mcpServers` JSON above, the `agentring` CLI, or the [REST API](#rest-api-n8n-scripts) |

Add [`skills/agentring`](../skills/agentring) where the harness reads skills, so the
agent knows when to message and when to call.

### You consult your agent from a call (`agents.yaml`)

```yaml
agents:
  claude:
    description: Claude Code in my web app's repo. Code questions, CI status.
    command: [claude, -p, --permission-mode, plan, --, "{task}"]
    cwd: ~/code/webapp
  codex:
    description: Codex in my web app's repo. Code questions, CI status.
    command: [codex, exec, --sandbox, read-only, --ephemeral, --, "{task}"]
    cwd: ~/code/webapp
  opencode:
    description: opencode in my web app's repo.
    command: [opencode, run, "Task: {task}"]
    cwd: ~/code/webapp
  hermes:
    description: My Hermes agent. Email, calendar, research.
    command: [hermes, -z, "Task: {task}"]
  openclaw:
    description: My OpenClaw assistant. Messages, calendar, home.
    command: [sh, -c, 'exec openclaw agent --agent main --session-id "agentring-${AGENTRING_CALL_ID:-test}" --message="$1"', sh, "{task}"]
```

Keep only the ones you use, and check each with `agentring agents ask <name> "…"`.
They start read-only or with the harness's own approval rules. Widen them on
purpose, knowing the voice AI acts on what it hears:

- **Claude Code**: `plan` can look but not change anything. To let it act, use
  `--permission-mode acceptEdits` with a narrow `--allowedTools` list.
- **Codex**: `--sandbox workspace-write` lets it edit files under `cwd`. Add
  `--skip-git-repo-check` if `cwd` is not a git repo.
- **opencode**: uses its permission config; don't add `--auto` (approves
  everything). With `opencode serve` running, `--attach http://localhost:4096`
  skips the start-up cost.
- **Hermes**: `-z` prints only the final answer. Don't add `--yolo` for the phone.

#### OpenClaw

`openclaw agent` runs one turn through your running gateway. The `sh -c` script is
fixed text: the task arrives as `$1` and is never parsed by the shell. The call id
gives each call its own OpenClaw session, so a second question on the same call
keeps the context of the first.

### Agents behind an HTTP API

For an agent that only runs as a server (an OpenAI-compatible endpoint such as the
OpenClaw gateway's or Hermes' API server, a webhook, your own service), point
`command` at a small script that reads the task on stdin and prints the answer:

```sh
#!/bin/bash
# ~/bin/ask-agent: task on stdin, answer on stdout. Set AGENT_URL, AGENT_MODEL, AGENT_KEY.
set -euo pipefail
jq -Rs --arg m "$AGENT_MODEL" --arg u "agentring-${AGENTRING_CALL_ID:-test}" \
  '{model: $m, user: $u, messages: [{role: "user", content: .}]}' \
| curl -sf "$AGENT_URL/v1/chat/completions" -H "Authorization: Bearer $AGENT_KEY" \
    -H 'content-type: application/json' -d @- \
| jq -er '.choices[0].message.content'
```

```yaml
  myagent:
    description: ...
    command: [~/bin/ask-agent]          # no "{task}": it goes to stdin
    env: {AGENT_URL: "http://127.0.0.1:8642", AGENT_MODEL: "...", AGENT_KEY: "..."}   # what your server expects
```

## REST API (n8n, scripts)

Every action is one HTTPS request to your relay with the daemon's `ak_…` API key
(`agentring status --json` prints the relay URL; the key is in
`~/.agentring/.env`). See [`protocol/relay.openapi.yaml`](../protocol/relay.openapi.yaml):

```bash
curl -s https://relay.example.com/v1/messages -H "Authorization: Bearer $AGENTRING_API_KEY" \
  -H 'content-type: application/json' -d '{"text": "Backup finished"}'
```

Replies come from `GET /v1/messages?after=<msg_id>`, or live over the WebSocket. A
call over plain REST only rings the phone: the voice needs a worker in the room,
which is the daemon (`agentring call --ai`, or the `call` MCP tool). See
[`examples/`](../examples) for an n8n workflow.
