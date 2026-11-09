# Setting up agentring

Goal: the user's Android phone gets your messages and rings when you call. Work through
these steps with the user. Ask before installing anything, and stop at each point that
needs them (installing the app, signing in, pairing the phone).

## Secrets

- Never invent an API key or a relay URL; ask the user.
- The API key (`ak_…`) gives access to the user's phone. Prefer that the user runs the
  command that stores it themselves, in a terminal on the machine where you run. If they
  only talk to you through a chat app, tell them the key will stay in that chat's history
  and that they can rotate it later (agentring.io dashboard, or a new key on their relay).
- Voice API keys (Gemini, OpenAI, …) go in `~/.agentring/.env`, never in a message or a
  call context.

## 1. The app

The user installs **agentring** from Google Play:
https://play.google.com/store/apps/details?id=dev.agentring.app (Android only).

## 2. Hosted or self-hosted?

Ask which one they want:

- **agentring cloud** (https://agentring.io): nothing to run. Sign in, pair the phone from
  the dashboard, copy the API key. The voice AI runs on agentring's servers. Plans have
  limits; a `quota_exceeded` error tells you where to upgrade. Go to **3a**.
- **Self-hosted**: they already run an agentring relay (docker compose; setup in
  https://github.com/mutonby/agentring/blob/main/docs/setup.md) and have its URL and API
  key. Go to **3b**. Don't deploy a server on your own initiative.

## 3a. agentring cloud

1. The user signs in at https://agentring.io, scans the dashboard's QR code with the app
   (Scan QR code), and copies the API key from the dashboard.
2. Connect the hosted MCP server. With the `agentring` command available (see 3b.1 to
   install it; the voice extra is not needed for the cloud):

   ```bash
   AGENTRING_API_KEY=ak_... agentring connect openclaw --url https://agentring.io/mcp   # or: hermes
   ```

   The command keeps the key out of your config files, registers the server with a
   30-minute tool timeout (a call lasts as long as the conversation) and installs this
   skill. Prefer that the user runs it themselves, so the key never passes through you.
   Without the `agentring` command, the user follows the manual steps on
   https://agentring.io/agents/openclaw or https://agentring.io/agents/hermes; don't
   edit your own configuration files for this.
3. Go to **4**.

## 3b. Self-hosted relay

1. Install the CLI (needs `uv`: https://docs.astral.sh/uv/; ask before installing it):

   ```bash
   uv tool install 'agentring[voice] @ git+https://github.com/mutonby/agentring#subdirectory=daemon'
   agentring version
   ```

2. Point it at the relay: `agentring init --relay https://relay.example.com --api-key ak_...`
3. Pair the phone:
   - At a terminal: `agentring pair` shows a QR code; the user scans it in the app (Scan QR code).
   - Over chat: run `agentring pair --json --no-wait` and send the user the `qr_payload`
     value. In the app they tap "Can't scan? Paste the code" and paste it. It works once and expires in 10
     minutes. Then check `agentring status --json` until `devices` lists the phone.
4. Calls need a voice provider key. The free option is Gemini
   (https://aistudio.google.com/apikey): the user adds `GEMINI_API_KEY=...` to
   `~/.agentring/.env`. Other providers: OpenAI, Grok, Phonic
   (https://github.com/mutonby/agentring/blob/main/docs/voice.md#providers).
5. Connect your harness:

   ```bash
   agentring connect openclaw     # or: agentring connect hermes
   ```

   This registers the local MCP server (`agentring mcp`, with a 30-minute tool timeout),
   installs this skill, and adds you to `~/.agentring/agents.yaml` so the user can ask you
   things during a call. `--dry-run` shows what would change; `--no-consult` skips
   `agents.yaml`. Running it again is safe.
6. Optional, if the user wants to call you from the app ("Call agent"): keep
   `agentring serve --ai` running (a terminal, tmux, or a service the user sets up). It
   answers the call with the voice AI, which can consult you through `agents.yaml`.
   For OpenClaw this needs the gateway running (`openclaw gateway`).

## 4. Check it works

1. Reload the MCP servers: OpenClaw picks the change up on the next turn (check with
   `openclaw mcp probe agentring`); in Hermes, run `/reload-mcp` or start a new session
   (check with `hermes mcp test agentring`).
2. Call the `status` tool: it should list the paired phone.
3. `send_message`: "agentring is connected ✓". Ask the user to confirm it arrived.
4. Offer a test call (`call` with a short `reason`); place it only if they say yes.

## When something fails

- `agentring: command not found`: `uv tool` puts it in `~/.local/bin`; add that to PATH
  or run `uv tool update-shell`.
- `401` / `unauthorized`: wrong or rotated API key.
- No phone in `status`: pairing didn't finish; pair again.
- Tool times out during a call: the harness's MCP timeout is too short; run
  `agentring connect …` again, or set it to 1800 s (`requestTimeoutMs: 1800000` in
  OpenClaw, `timeout: 1800` in Hermes).
- Docs: https://agentring.io (Agents menu) and https://github.com/mutonby/agentring
