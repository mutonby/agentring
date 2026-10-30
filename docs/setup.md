# Setup

At the end of this guide your phone rings when your agent calls it, even locked, and you
can call your agent back from the app. It takes about 30 minutes the first time.

There are three pieces:

| Piece | Where it runs | What it does |
|---|---|---|
| **relay** + **LiveKit** | a VPS, or your own computer | pairing, messages, call state, push; LiveKit carries the call audio |
| **daemon / CLI** (`agentring`) | the computer where your agent runs | the CLI, the MCP server for your agent, the voice AI; only makes outgoing connections, so NAT is fine |
| **Android app** | your phone | rings, native call screen, messages |

First choose where the relay runs:

| | **A. A VPS with a domain** | **B. Your own computer** |
|---|---|---|
| Works when | the phone is anywhere with internet | the phone is on the same Wi-Fi, or anywhere over [Tailscale](https://tailscale.com) |
| You need | a Linux server with Docker (a €4 VPS is plenty) and a DNS name | Docker on macOS, Linux or Windows, and the computer switched on |
| Security | HTTPS with an automatic certificate | plain HTTP, accepted only on private and Tailscale addresses |

Already running Caddy, Traefik, nginx or Coolify on your server? Use option A with
[an existing reverse proxy](#behind-an-existing-reverse-proxy-with-calls).

## 1. Firebase: what wakes the phone (5 minutes, free)

A locked phone, or one where the app is closed, only rings if a push notification wakes
it. agentring uses **your own** free Firebase project for that: follow
[firebase.md](firebase.md) and you end up with two files. Put both in `deploy/secrets/`
of your checkout:

```
deploy/secrets/firebase-service-account.json
deploy/secrets/google-services.json
```

Without them, messages and calls only reach the phone while the app is open on screen.

## 2. The relay

```bash
git clone https://github.com/mutonby/agentring && cd agentring/deploy
```

### Option A: a VPS with a domain

1. Point a DNS record (e.g. `relay.example.com`) at the server.
2. Open these ports, in the server's firewall and in any cloud firewall in front of it
   (Docker-published ports bypass `ufw`):
   - TCP 80 and 443: Caddy and its Let's Encrypt certificate;
   - UDP 50000–50100 and TCP 7881: call audio (LiveKit).
3. Configure and start:

   ```bash
   cp .env.example .env
   nano .env      # AGENTRING_DOMAIN, AGENTRING_AGENT_NAME, and uncomment the two FCM_ lines
   docker compose up -d --build
   docker compose logs relay | grep -A2 "API key"
   ```

4. Check it from your computer: `curl https://relay.example.com/healthz` prints `{"ok":true}`.

`LIVEKIT_NODE_IP` is the server's public IPv4; leave it empty to let LiveKit find it over
STUN. The LiveKit secret is generated on first start inside a Docker volume and never goes
through `.env`.

### Option B: your own computer

1. Find the address the phone will use to reach this computer:
   - same Wi-Fi: the LAN IP, e.g. `ipconfig getifaddr en0` (macOS) or `hostname -I` (Linux);
   - away from home too: install [Tailscale](https://tailscale.com) on the computer and the
     phone, sign in to the same account on both, and use `tailscale ip -4` (a `100.x` address).

   On a LAN, give the computer a fixed IP (a DHCP reservation in your router), or the
   phone loses the relay when the IP changes. Tailscale addresses don't change.
2. Configure and start:

   ```bash
   cp .env.local.example .env.local
   nano .env.local   # AGENTRING_HOST_IP, AGENTRING_AGENT_NAME, and uncomment the two FCM_ lines
   docker compose -f docker-compose.local.yml --env-file .env.local up -d --build
   docker compose -f docker-compose.local.yml logs relay | grep -A2 "API key"
   ```

3. Check it from the phone's browser: `http://<AGENTRING_HOST_IP>:8080/healthz` shows
   `{"ok":true}`. If it doesn't load, allow incoming connections for Docker in the
   computer's firewall.

Everything is published on that one address, not on all interfaces: the relay on 8080,
LiveKit on 7880 and 7881, and the call audio on UDP 50000–50100.

### The API key

On first start the relay prints its **API key** (`ak_…`) once. You can read it again any
time with `docker compose exec relay cat /data/api_key` (add
`-f docker-compose.local.yml --env-file .env.local` for option B). It is the key to your
relay: give it only to the daemon.

## 3. The daemon and CLI

On the computer where your agent runs (it can be the same one as the relay). You need
Python 3.11 or later and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install './daemon[voice]'        # from the repo checkout; [voice] is needed for calls
agentring init --relay https://relay.example.com --api-key ak_...   # option B: http://<AGENTRING_HOST_IP>:8080
agentring status
```

Its configuration lives in `~/.agentring/` (`config.yaml`, and the key in `.env`, mode 600).
The `AGENTRING_RELAY_URL` and `AGENTRING_API_KEY` environment variables override it. To run
the daemon on the VPS instead, next to the relay, see the `daemon` profile at the top of
`deploy/docker-compose.yml`.

## 4. The app

1. On the phone, download `agentring.apk` from the
   [latest release](https://github.com/mutonby/agentring/releases) and install it (Android
   asks you to allow installs from your browser once).
2. Open it and tap **Get started**. It asks, one by one, for notifications, the microphone,
   full-screen calls on the lock screen and unrestricted battery use: allow them all.
3. On your computer, run `agentring pair`. Scan the QR code it shows with the app, or copy
   the pairing code it prints and paste it in the app.
4. The CLI prints `Paired with <your phone> ✓`, and `agentring status` shows `push ok` next
   to it. If it says `NO push token`, the relay has no Firebase files (step 1).
5. In the app, **Settings → Calls** lists what calls need. Fix anything that is not OK.

## 5. Your first message and call

```bash
agentring msg "hello"                 # the phone gets a notification; reply from it
agentring replies                     # your reply

agentring call "test"                 # rings; answer and you hear a greeting, then your own voice echoed
echo GEMINI_API_KEY=... >> ~/.agentring/.env   # free key: https://aistudio.google.com/apikey (or another provider: voice.md)
agentring call "The backup failed" --ai        # a voice AI tells you why it's calling and talks with you
```

Now lock the phone, wait a minute and run `agentring call "test" --ai` again: it should
ring like a normal call. If it doesn't, see [troubleshooting-oem.md](troubleshooting-oem.md).

To call your agent from the app ("Call agent"), keep `agentring serve --ai` running on the
computer.

## 6. Connect your agent

```bash
claude mcp add agentring -- agentring mcp   # Claude Code; Codex, opencode, OpenClaw, Hermes: docs/agents.md
```

Then ask it, for example: *"Run the migration; if anything fails, call me."* Recipes for
other harnesses, and how to let the voice AI ask your agents for help during a call:
[agents.md](agents.md).

## When something doesn't work

| Symptom | Check |
|---|---|
| The app won't accept the relay URL | It only accepts `http://` for private, LAN and Tailscale addresses; anything else must be `https://`. |
| Pairing fails | Open `<relay URL>/healthz` in the phone's browser. If it doesn't load, the phone can't reach the relay (Wi-Fi, Tailscale, firewall). |
| Messages arrive only with the app open | `agentring status` says `NO push token`: put the Firebase files in `deploy/secrets/`, uncomment the `FCM_` lines, restart, and pair again. |
| The phone doesn't ring when locked | Firebase (above), then the battery settings for your phone: [troubleshooting-oem.md](troubleshooting-oem.md). |
| The call connects but there is no sound | Call audio needs UDP 50000–50100 and TCP 7881 open to the phone. On a VPS, set `LIVEKIT_NODE_IP` to its public IPv4. |
| `agentring call --ai` says it needs a key | The provider's key in `~/.agentring/.env`: `GEMINI_API_KEY` by default, or `OPENAI_API_KEY` / `XAI_API_KEY` / `PHONIC_API_KEY` ([voice.md](voice.md#providers)). |

### Running the relay without Docker (development)

```bash
cd relay && npm ci && npm run build
AGENTRING_INSECURE_DEV=1 AGENTRING_PUBLIC_URL=http://192.168.1.20:8080 npm start
```

This covers messages only. Calls also need a LiveKit server (`LIVEKIT_URL`,
`LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`); option B sets that up for you.

## Behind an existing reverse proxy (with calls)

Use `deploy/docker-compose.proxy.yml` when the server already runs Caddy, Traefik
or nginx on 80/443 (it is also what a Coolify "Docker Compose" app points at).

What it exposes: the relay on `127.0.0.1:8095` and LiveKit signalling on
`127.0.0.1:7880` (both reachable only through your proxy), plus LiveKit media on
**UDP 50000–50100 and TCP 7881** on all interfaces. Open those in any cloud
firewall in front of the server (Docker-published ports bypass `ufw`).

Hardening built in: both containers run as unprivileged users with a read-only
root filesystem, no Linux capabilities, `no-new-privileges` and CPU/memory/pid
limits; the relay image is pinned by digest, built without npm install scripts
and ships without npm/yarn; **no secret is passed as an environment variable**.

1. On the server, as root, with the two Firebase files in a directory:

   ```bash
   deploy/prepare-host.sh /path/to/firebase-files   # prints LIVEKIT_API_KEY=lk_…
   ```

   It creates `/opt/agentring/relay` (Firebase files + LiveKit secret, uid 1000)
   and `/opt/agentring/livekit` (config + `keys.yaml`, uid 65534). The LiveKit
   secret is generated there and never leaves the server.

2. Environment for the compose app (none of these is secret):
   `AGENTRING_DOMAIN`, `LIVEKIT_NODE_IP` (the server's public IPv4),
   `LIVEKIT_API_KEY` (from step 1), optionally `AGENTRING_AGENT_NAME`.

3. Proxy routes. Caddy example:

   ```caddyfile
   agentring.example.com {
   	header Strict-Transport-Security "max-age=31536000"
   	header -Server
   	# LiveKit's admin API and the relay's webhook are internal only.
   	handle /twirp/* {
   		respond 404
   	}
   	handle /v1/livekit/webhook {
   		respond 404
   	}
   	handle /rtc* {
   		reverse_proxy 127.0.0.1:7880
   	}
   	handle {
   		reverse_proxy 127.0.0.1:8095
   	}
   }
   ```

4. The relay generates its API key on first start; read it with
   `docker exec <relay container> cat /data/api_key` and give it only to the daemon
   (`agentring init`).
