# agentring cloud (hosted mode)

agentring stays self-hosted and free. **agentring cloud** is the same software run by
the maintainer for people who don't want to run a server: install the app, sign in on
the web, scan a QR code, add one MCP URL to your agent. No Firebase project, no VPS,
no LiveKit, no voice API key.

Three pieces, two repositories:

| Piece | Where | License |
|---|---|---|
| **Hub mode** of the relay: many isolated tenants in one relay process | this repo, `relay/src/hub/` | Apache-2.0 |
| **Remote MCP server** (Streamable HTTP) with hosted voice AI | this repo, `daemon/` (`agentring mcp-http`) | Apache-2.0 |
| **Control plane**: landing, waitlist, accounts, dashboard, Stripe billing | private repo `agentring-cloud` | proprietary |

Hub mode and the remote MCP server are useful on their own to self-hosters (a team
relay with one tenant per person; one MCP URL instead of a local daemon).

```
 phone app ──https──▶  /t/{tenant_id}/v1/...  ┐
 agent (daemon) ─────▶ /t/{tenant_id}/v1/...  ├─ relay in hub mode ── tenants/{id}/relay.db
 control plane ──────▶ /admin/v1/...          ┘        │ shared FCM + LiveKit
 LiveKit ────────────▶ /v1/livekit/webhook (routed by room name)
 agent (MCP client) ─▶ /mcp  ── agentring mcp-http ── /t/{tenant_id}/v1/... (tenant API key)
```

## Hub mode (relay)

Enabled with `AGENTRING_HUB=1`. Each tenant is a full relay instance (its own SQLite
file, relay id, JWT secret, API key, event log, calls) mounted under a path:

- **Tenant base URL:** `{AGENTRING_PUBLIC_URL}/t/{tenant_id}`. Everything under it is
  the normal relay API from `protocol/relay.openapi.yaml`, unchanged. The QR code's
  `relay_url` is the tenant base URL, so the app and the daemon need no changes.
- **Tenant id:** `tnt_` + ULID (`^tnt_[0-9A-HJKMNP-TV-Z]{26}$`).
- **Data:** `{AGENTRING_DATA_DIR}/hub.db` (tenant registry, usage) and
  `{AGENTRING_DATA_DIR}/tenants/{tenant_id}/relay.db`.
- **Shared services:** one FCM project (the operator's; `push_config` comes back in
  the pair response as today) and one LiveKit server. LiveKit room names are
  `{tenant_id}.{call_id}`; the hub receives the LiveKit webhook at
  `/v1/livekit/webhook` and hands it to the tenant named in the room.
- **Tenants are loaded lazily** on first request and unloaded after 15 min without
  requests, open sockets or open calls. On start the hub loads the tenants that have a
  ringing or active call, so their timers keep running.
- **Disabled tenant:** every tenant request gets `403 tenant_disabled`.
- **No tenant-less API:** `/v1/*` (other than the LiveKit webhook) answers 404 in hub mode.

### Limits (quotas)

Per tenant, set by the admin API; `null` = unlimited (the default).

| Limit | Enforced at | Error |
|---|---|---|
| `max_devices` | `POST /v1/pair` (active, non-revoked devices) | `402 quota_exceeded` |
| `call_seconds_per_month` | `POST /v1/calls`, and an active call is ended (`end_reason: "quota"`) when it runs out | `402 quota_exceeded` |
| `messages_per_day` | `POST /v1/messages` (agent and user messages, UTC day) | `402 quota_exceeded` |

Call seconds = answered time (`answered_at` → `ended_at`) of the tenant's calls in the
current UTC calendar month.

### Admin API

`Authorization: Bearer {admin token}` (`AGENTRING_HUB_ADMIN_TOKEN_FILE`, or
`AGENTRING_HUB_ADMIN_TOKEN`; at least 32 characters). Only meant for the control
plane on an internal network: requests that came through the public proxy
(`X-Forwarded-For` present) get 404, like the LiveKit webhook. Errors use the relay's
`{"error": {"code", "message"}}` shape.

```
Limits  = { max_devices: int|null, call_seconds_per_month: int|null, messages_per_day: int|null }
Usage   = { month: "2026-11", call_seconds: int, calls: int, messages_today: int, devices: int }
Tenant  = { tenant_id, base_url, agent_name, limits: Limits, disabled: bool, created_at: ms, usage: Usage }
```

| Method & path | Body | Response |
|---|---|---|
| `POST /admin/v1/tenants` | `{agent_name?, limits?}` | `201 {tenant: Tenant, api_key}` — the key is shown only here and on rotation |
| `GET /admin/v1/tenants/{tid}` | | `200 Tenant` |
| `PATCH /admin/v1/tenants/{tid}` | `{agent_name?, limits?, disabled?}` (limits merge per field) | `200 Tenant` |
| `POST /admin/v1/tenants/{tid}/api-key` | | `200 {api_key}` (old key stops working at once) |
| `DELETE /admin/v1/tenants/{tid}` | | `204` (data deleted) |
| `POST /admin/v1/tenants/{tid}/pairing-tokens` | `{ttl_s?}` | `201 PairingToken` (same shape as `/v1/pairing-tokens`) |
| `GET /admin/v1/tenants/{tid}/devices` | | `200 {devices: Device[]}` |
| `DELETE /admin/v1/tenants/{tid}/devices/{device_id}` | | `204` |
| `POST /admin/v1/resolve-key` | `{api_key}` | `200 {tenant_id, base_url}`, `404 not_found`, or `403 tenant_disabled` |

## Remote MCP server

`agentring mcp-http` serves the same tools as `agentring mcp` (`send_message`,
`get_replies`, `call`, `status`) over MCP Streamable HTTP at `POST /mcp`.

- **Auth:** `Authorization: Bearer ak_…` (the tenant's relay API key); anything else
  gets `401` with `WWW-Authenticate: Bearer`.
- **Which relay:** with `AGENTRING_HUB_ADMIN_URL` + admin token
  (`AGENTRING_HUB_ADMIN_TOKEN_FILE` or `AGENTRING_HUB_ADMIN_TOKEN`), the key is resolved
  to `{tenant_id, base_url}` with `POST /admin/v1/resolve-key` (positive answers cached
  5 minutes by a hash of the key; `404` → `401` to the client; the hub unreachable or
  refusing the admin token → `503`). The admin URL must be the relay's internal
  address (the admin API hides from proxied requests). If `AGENTRING_RELAY_URL` is also
  set, tenants are reached at `{AGENTRING_RELAY_URL}/t/{tenant_id}` (internal) instead
  of the public `base_url`. Without a hub, every key is checked against the one relay
  in `AGENTRING_RELAY_URL` with `GET /v1/status` (self-hosted).
- **Voice:** `call` runs the voice AI on the server (provider and key from the server's
  environment, as for `agentring call --ai`), with a global cap on concurrent calls
  (`AGENTRING_MCP_MAX_CALLS`, default 4; over it the tool error is `busy`). The
  relay's own one-call-per-tenant rule still applies. `agents.yaml` is never used.
- **Long requests:** `call` and a waiting `get_replies` answer as `text/event-stream`
  with an SSE keep-alive comment every 15 s.
- **State:** per tenant, the daemon's usual SQLite mirror (`Store`, synced with the
  REST events API) in `{AGENTRING_MCP_DATA_DIR}/tenants/{tenant_id}.db`; it records
  which replies `get_replies` already returned, shared by all the tenant's sessions.
- **Sessions:** `Mcp-Session-Id` from `initialize`, in memory (a restart means
  clients re-initialize; the per-tenant state is on disk). `DELETE /mcp` ends one.
- Install in Claude Code:
  `claude mcp add --transport http agentring https://{domain}/mcp --header "Authorization: Bearer ak_…"`

All options: [agents.md](agents.md#remote-mcp-http). OAuth (for claude.ai custom
connectors) is a later step.

## Control plane (private repo)

Landing page with waitlist and pricing, magic-link sign-in, one tenant per account
(created on first sign-in through the admin API), dashboard (QR pairing, devices,
API key rotation, MCP install command, usage vs. plan), Stripe Checkout + customer
portal + webhook that sets the tenant's limits.

### Plans

| | Free | Pro — $9/month |
|---|---|---|
| Phones | 1 | 5 |
| Call minutes / month (incl. hosted voice AI) | 10 | 200 |
| Messages / day | 100 | 2000 |
| Self-hosted | always free, no limits | |

Limits as admin-API values — Free: `{max_devices: 1, call_seconds_per_month: 600,
messages_per_day: 100}`; Pro: `{max_devices: 5, call_seconds_per_month: 12000,
messages_per_day: 2000}`.

## Deploy

One Coolify project: relay (hub mode), LiveKit, `agentring mcp-http` and the control
plane, on one hostname: `/t/*`, `/v1/livekit/webhook` (internal only) → relay;
`/rtc*` → LiveKit; `/mcp` → MCP server; everything else → control plane.
