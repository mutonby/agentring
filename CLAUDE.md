# agentring — notes for Claude Code sessions

Open-source project (Apache-2.0) that lets any AI agent (Claude Code, OpenClaw,
n8n, scripts) **call the user's phone over VoIP and message it**, and lets the
user call the agent back. The phone must ring like a real call when locked. No
PSTN, no numbers, no Twilio. F1 scope: one user, one agent, Android only,
self-hosted, no accounts.

## Working agreement with the user (important)

- The user writes in **Spanish**; answer in Spanish. **Code, comments, docs and
  README in English.**
- Work **milestone by milestone (M1→M5)**. At the end of each milestone: stop,
  explain how to test it, and **wait for the user's OK** before starting the next.
- **Ask before deviating** from the design in `docs/architecture.md` /
  `docs/protocol.md`. Record accepted deviations there.
- The protocol contract is `protocol/relay.openapi.yaml` (single source of
  truth). Change it first, then the implementations, and keep
  `protocol/examples/` fixtures valid.
- Develop on `main` (the default branch; renamed from
  `claude/agentring-voip-calls-l4ei3s`). Commit with clear messages; don't open PRs
  unless asked. Only `main` pushes replace the `android-preview` APK.

## Status

| Milestone | Scope | Status |
|---|---|---|
| **M1** | relay + pairing + messages + push | **Done**; verified on the user's Pixel 6 (Android 16), FCM incl. forced Doze (~1.5 s). |
| **M2** | phone rings locked / Doze in < 5 s with native call UI; audio (echo/tone is fine) | **In progress**: works on the Pixel 6 (locked + Doze → call screen in 1.6 s, in the room ~0.5 s after answer). Group calls + "join the call" while active (`POST /v1/calls/{id}/join`, Join notification/banner; not yet tested on a real phone). TODO: LiveKit in deploy compose, user's listening test, OK. |
| M3 | Gemini Live conversation (< 1 s turns), OpenAI Realtime optional; `consult_agent` with `backend: command` | **In progress** (merged in #2): generic `agentring.voice` library (VoiceAgent, pluggable `RealtimeBackend`, Python/MCP tools, hooks), `call --ai`, `serve --ai` answering the phone; live e2e tests pass (docs/voice.md). Providers: Gemini Live (default), OpenAI Realtime, Grok Voice, Phonic (`--provider` / `AGENTRING_VOICE_PROVIDER`, issue #12); live e2e passes with Gemini, Grok and Phonic, OpenAI only against fake servers so far. `consult_agent` + `agents.yaml` done (docs/agents.md; slow answers come as a note, or a message after the call). TODO: OpenAI live run, transcripts in the store, real-phone test. |
| M4 | call the agent from the app; missed/declined fallbacks | **In progress**: app side done on branch `m4-call-agent` ("Call agent" → `POST /v1/calls`, joins the room at once, "Calling…" until `call.accept` / agent in the room / poll, cancel while ringing, agent offline / busy / declined / missed messages; not yet tested on a real phone). Agent side: `serve --ai` answers. Missed → `msg.new` fallback done for AI calls (`call --ai`, MCP `call`). |
| M5 | MCP server, skill, examples, docs, < 15 min quickstart with `docker compose up` | **In progress**: `agentring mcp` (stdio, no SDK), `skills/agentring`, examples (claude-code, n8n), LiveKit + optional daemon in the basic compose, release workflow (signed APK, PyPI, GHCR; docs/release.md), SECURITY/CONTRIBUTING, OEM guide. OpenClaw example + per-harness recipes (docs/agents.md: Claude Code, Codex, opencode, OpenClaw, Hermes, HTTP agents). First release v0.2.0 published 2026-10-28 (release key in GitHub secrets; the maintainer keeps the keystore offline; PyPI job off until AGENTRING_PUBLISH_PYPI=true). Repo public since 2026-10-28: private vulnerability reporting, secret scanning + push protection, Dependabot alerts, rulesets (main: no force-push/deletion; CI required for PRs, admins bypass; v* tags admins only), fork PR workflows need approval. TODO: GHCR packages public (web UI), PyPI trusted publishing. |

Agreed direction for M3: keep agentring agnostic with two plug points — the
voice backend (Gemini Live, OpenAI Realtime, Grok Voice, Phonic; later a local
OpenAI-Realtime-compatible server such as Hugging Face `speech-to-speech` via
`OpenAIRealtime(url=…)`) and the tools (any local MCP
server, e.g. jefe-mcp on :8787, plus the built-in `consult_agent`). Over the
phone, only an allowlist of tools. The user's own agent in production
(`tonvi-agentes` on Coolify) talks on calls with **Grok Voice**; its
`VOICE_PROVIDER=openai` is only the Telegram voice-note transcription.

Dependency policy (user asked for care after recent supply-chain attacks):
prefer releases at least ~2 weeks old, pin exact versions, check the lockfiles
against OSV before pushing. Apply age limits per package (`uv lock
--exclude-newer-package`), never globally: a global `--exclude-newer` once
downgraded certifi to a version with compromised CA roots.

## Repository layout

```
protocol/       relay.openapi.yaml (REST + event schemas), examples/ (event fixtures, push signature vector)
relay/          TypeScript, Fastify 5, node:sqlite (no native deps), jose; FCM HTTP v1 without firebase-admin
daemon/         Python ≥3.11 + uv; `agentring` CLI (typer), local SQLite mirror synced from the relay
app-android/    :core = pure Kotlin/JVM (protocol models, PushVerifier, RelayApi, RelaySocket)
                :app  = Compose/Material 3 app, package dev.agentring.app, minSdk 26, targetSdk 35
deploy/         docker-compose.yml (caddy + relay + LiveKit), docker-compose.local.yml (own computer, LAN/Tailscale, no TLS),
                docker-compose.proxy.yml (behind an existing proxy), Caddyfile, .env.example, .env.local.example
scripts/        ci-emulator-e2e.sh, device-test.sh
docs/           architecture.md, protocol.md, setup.md, firebase.md
.github/        workflows/ci.yml (relay+daemon tests, compose smoke), workflows/android.yml (APK + emulator e2e + release)
```

Release APKs are signed with the release key (fingerprint in SECURITY.md); android-preview
stays a debug-key test build and does not install over a release APK (uninstall first).

## Build and test

```bash
cd relay  && npm ci && npm run typecheck && npm test && npm run build   # 35 tests, Node >= 22.13
cd daemon && uv run --extra voice pytest -q   # needs relay/dist (e2e against the real relay)
cd app-android && ./gradlew :core:test :app:assembleDebug
cd app-android && ./gradlew -PcoreOnly :core:test   # when Google's Maven is unreachable
```

CI (`.github/workflows/android.yml`) builds the APK, starts a relay, boots an
API 34 emulator, runs `M1EndToEndTest` (pair via UI → WS message → read receipt
→ reply in app → signed push → notification inline reply), checks
`agentring replies`, uploads screenshots, and replaces the `android-preview`
pre-release with `agentring.apk`.

Local real-phone test: `scripts/device-test.sh [apk]` or `--instrumented`.

## Key design decisions (all accepted by the user)

- Commands are REST; events flow over a resumable WebSocket (`/v1/ws?after_seq=`)
  from a persisted event log with a global `seq`; FCM carries only what must wake
  the phone.
- LiveKit tokens are **not** in `call.invite`; the callee gets them from
  `POST /v1/calls/{id}/accept` (or `/join`), each phone its own. The caller gets its own in the
  `POST /v1/calls` response.
- FCM data payloads are signed: `sig = HMAC-SHA256(push_secret, event_json)` plus
  `relay_id`; the app rejects anything else.
- **Firebase is configured at runtime**: the relay reads the user's
  `google-services.json` and returns `push_config` in the pair response, so one
  prebuilt APK works with anyone's Firebase project.
- Extra call state `canceled` and informational event `call.ringing`; one call at
  a time (`409 busy`); group calls: every phone rings, any may answer while it
  rings, and any paired phone not on an active call may join it later
  (`POST /v1/calls/{id}/join`; `call.accept` is pushed at normal priority so the
  other phones can show "Join"). Targeted calls: the agent may pass `device_ids`
  to ring only those phones; the call is then private to them (the relay filters
  its events, pushes and REST views for every other phone).
- Auth: daemon uses an `ak_…` API key (generated on first relay start, stored in
  `DATA_DIR/api_key`); devices use a long-lived HS256 `device_jwt` checked
  against the DB on every request (immediate revocation, WS closed with 4401).
- The app accepts `http://` relays only on private/LAN/localhost hosts
  (`RelayUrlPolicy`); everything else must be https.
- Debug keystore `app-android/app/debug.keystore` is committed on purpose so CI
  builds install over each other. Release builds take the real key from
  `AGENTRING_KEYSTORE_*` (docs/release.md) and fall back to the debug key locally.
- M3: `consult_agent` command backend must take an **argv list**, never a shell
  string (the task text comes from the voice model).
- M2 plan: one hostname, Caddy routes `/v1/*` → relay and `/rtc*`, `/twirp*` → LiveKit.

## Environment gotchas

- **Cloud sessions** (claude.ai/code) cannot reach `dl.google.com` /
  `maven.google.com`, so the Android app can't be built there. Use the
  `-PcoreOnly` build locally and let GitHub Actions build/test the app (read logs
  with the GitHub MCP tools; artifacts/release assets can be fetched via
  `gh api -H "Accept: application/octet-stream" repos/mutonby/agentring/releases/assets/<id>`).
  A cloud session also cannot see a USB phone; for that the user runs Claude Code
  on their own computer.
- An Android SDK was unpacked to `/opt/android-sdk` in one cloud session (from the
  `cimg/android` Docker image); it does not persist across sessions.
- Docker builds inside the cloud sandbox can't reach npm; the compose stack is
  verified by the `deploy-smoke` CI job instead.
- `ubuntu-latest` runners need disk freed for the emulator (already in the workflow).
- Local M2 testing (see `build-local/`, git-excluded): relay on :8080, LiveKit in
  Docker (`livekit/livekit-server:v1.13.10`, `node_ip` = the Mac's LAN IP, UDP
  50000-50200) because the macOS firewall blocks the Homebrew `livekit-server`
  binary; WebRTC ignores loopback candidates, so `adb reverse` can't carry media.
  The phone reaches the relay and LiveKit signalling via `adb reverse tcp:8080`
  and `tcp:7880` (wireless adb works).
- Espresso < 3.7 crashes on Android 16 (`InputManager.getInstance`); the
  instrumented test needs the phone unlocked.
- Core-Telecom `disconnect()` only accepts LOCAL/REMOTE/MISSED/REJECTED; any other
  cause throws and leaves a ghost RINGING call that blocks later calls
  (`CallException 5`). `TelecomBridge.platformCause` maps them.

## What M2 needs (from the original spec)

- Relay: calls endpoints (already in the OpenAPI), ring timer (30 s → `call.missed`),
  LiveKit room + per-call tokens (TTL 120 s, one room), LiveKit webhook, FCM push of
  `call.invite` (TTL = until `expires_at`) and of accept/decline/end/missed for ringing calls.
- Daemon: `agentring call "reason" [--urgent] [--wait]`, voice worker (LiveKit Agents)
  joining the room — echo/tone is enough for M2.
- App: self-managed `ConnectionService` + `PhoneAccount` (`MANAGE_OWN_CALLS`), CallStyle
  notification with full-screen intent, Android 14+ FSI declaration, urgent priority →
  alarm stream + bypass DnD when policy access granted, LiveKit Android SDK audio,
  active-call screen (mute, speaker, hang up), full onboarding (notifications, mic,
  FSI, DnD, battery optimisation with Xiaomi/Samsung/Huawei instructions, test call).
- Acceptance: phone locked, screen off, 10 min in Doze → `agentring call "test"` rings
  in < 5 s with native call UI, answer, audio works.
- Deploy: add livekit-server to compose (UDP 50000–60000, TCP 7881).
