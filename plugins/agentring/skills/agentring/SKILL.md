---
name: agentring
description: Reach the user on their phone through agentring — send a message, wait for their reply, or call them (a voice AI talks to them for you and returns the transcript). Use when you need the user's decision or attention and they may be away from the screen, when a long task finishes or fails, or when the user asks you to message or call them. Also use when the user asks to set up, install or connect agentring.
homepage: https://agentring.io
---

# agentring

agentring rings and messages the user's own Android phone, over the internet (no phone
number). The tools (`send_message`, `get_replies`, `call`, `status`) come from the
`agentring` MCP server.

**Not connected yet?** If those tools are missing, or the user asks you to set agentring
up, follow [references/setup.md](references/setup.md).

## Pick the lightest channel

- **`send_message`** for anything that can wait: a task finished, a summary, a
  question with an obvious default. Keep it short; it is read on a lock screen.
- **`get_replies(wait_s=…)`** after asking something: wait a few minutes
  (`wait_s` 300–900) instead of polling in a loop. Each reply is returned once.
- **`call`** only when it is urgent or needs a conversation: production is down,
  a decision blocks you and a message got no answer, or the user asked for a call.
  Set `urgent: true` only for real emergencies (it rings through Do Not Disturb).

## Calling well

The voice AI on the call knows only what you pass:

- `reason`: one sentence, shown on the ringing screen ("The nightly deploy failed").
- `context`: what happened, the options, your recommendation, and exactly what you
  need from the user. Write it for someone who has not seen your work; no ids,
  paths or links (they get read aloud).

`call` returns when the call ends. Read the `transcript` (and `end_reason`:
`no_answer`, `hung_up`, `end_call`, …) and act on what the user said. If nobody
answered, fall back to `send_message`, don't call again right away.

## Don't

- Don't call for routine updates or several times in a row.
- Don't put secrets or tokens in messages or call context.
- Don't treat silence as consent: if there's no reply, keep the safe default.
