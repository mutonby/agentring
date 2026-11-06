# Browser client

The relay serves a small browser client at **`https://<your relay>/web/`**. It pairs
the browser as one more device and does for calls what the Android app does, while
the tab is open:

- rings when the agent calls (ringtone, tab title, a desktop notification when the tab
  is in the background), answer or decline;
- "Call agent", like the app's button;
- "Join" a group call already in progress on other devices;
- mute, hang up, and listen-only mode when there is no microphone.

It is plain HTML, CSS and JavaScript in [`relay/web/`](../relay/web), with no build step
and no framework. The LiveKit browser SDK (`livekit-client`, pinned in
`relay/package-lock.json`) is copied into `relay/web/vendor/` by `npm run build` and
served by the relay itself, not from a CDN.

## Using it

1. Open `https://<your relay>/web/` in a desktop or mobile browser.
2. On the computer where your agent runs: `agentring pair`. It prints a pairing code
   under the QR code (`{"v":1,"relay_url":…,"pairing_token":"pt_…"}`).
3. Paste the code into the page, name the browser and press **Pair**. The browser now
   appears in `agentring devices` with platform `web`.
4. Click anywhere on the page once: browsers only play sound (the ringtone) after a
   click. Allow notifications if you want to see calls while the tab is in the
   background.

Try it: `agentring call "Testing the browser client"`.

To unpair, press **Forget this browser** and revoke it on the relay too:
`agentring devices revoke <device_id>`.

## Limits

- **It only rings while the tab is open.** There is no push: events arrive over the
  relay's WebSocket. For a phone that rings when locked, use the Android app.
- **The microphone needs a secure context**: HTTPS, or `http://localhost`. With the
  local compose (`http://<LAN IP>:8080`) the page works but can only listen; use the
  HTTPS deployment, `localhost`, or HTTPS through Tailscale (`tailscale serve`).
- **The credentials live in the browser's `localStorage`**: the device JWT of this
  browser. Anyone who can run JavaScript on the relay's origin or read the browser
  profile can use it until it is revoked. The page has a strict Content Security
  Policy (only its own scripts; it can only connect to the relay and its LiveKit
  server) and the relay serves nothing else under `/web/`.

## How it talks to the relay

Same origin as the API, so no CORS. Everything is the existing protocol
([protocol.md](protocol.md)) with two additions:

- `POST /v1/pair` accepts `platform: "web"`. A web device has no FCM token, so the relay
  never pushes to it; it gets everything from the WebSocket.
- Browsers can't set `Authorization` on a WebSocket, so the client offers the
  subprotocols `agentring.v1` and `bearer.<device_jwt>`; the relay authenticates with
  the second one and answers with `agentring.v1`. The header still wins when present.

The call flow mirrors the Android app: `call.invite` → `POST /ringing`, answer with
`POST /accept` (or `/join` if the ring window closed meanwhile) → join the LiveKit room
with the returned token and publish the microphone; "Call agent" is `POST /v1/calls`
with an empty body, joining the room at once and polling `GET /v1/calls/{id}` until the
agent picks up. After a reconnect the stream resumes from the last `seq`, and a replayed
`call.invite` only rings if `GET /v1/calls/{id}` says it still rings.
