# agentring daemon

The agent side of [agentring](https://github.com/mutonby/agentring): the `agentring`
CLI, the MCP server (`agentring mcp`) and the voice AI for calls. It lets your AI
agents message and call your phone, and lets you call them back.

```bash
uv tool install 'agentring[voice]'   # or, from a checkout: uv tool install './daemon[voice]'
agentring init --relay https://relay.example.com --api-key ak_...
agentring pair                       # scan the QR with the Android app
agentring msg "hola"
agentring call "Testing agentring" --ai   # needs GEMINI_API_KEY in ~/.agentring/.env
claude mcp add agentring -- agentring mcp
```

Your API key must not cross the internet in clear, so a plain `http://` relay is refused
unless its host is loopback, a private or CGNAT (Tailscale) address, a `.local` name or a
single-label name such as `relay` in Docker. `AGENTRING_ALLOW_INSECURE_RELAY=1` lifts this,
for a network you trust that doesn't look like one. The local database is readable by you only.

You need your own relay (self-hosted, Docker compose) and the Android app. See the
[setup guide](https://github.com/mutonby/agentring/blob/main/docs/setup.md)
and [connecting agents](https://github.com/mutonby/agentring/blob/main/docs/agents.md).
