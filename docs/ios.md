# iPhone app (app-ios)

Native Swift app with the same features as the Android app (Android Auto aside):
pair by QR or pasted code, incoming calls that ring like a phone call with the iPhone
locked (CallKit + PushKit), call the agent, join a call in progress, messages with
reply from the notification. Bundle id `dev.agentring.app`, iOS 17+.

## Layout

```
app-ios/
  project.yml            XcodeGen spec (the .xcodeproj is generated, not committed)
  Config/Signing.xcconfig  DEVELOPMENT_TEAM comes from Config/Local.xcconfig (not committed)
  AgentringCore/         Swift package, no UIKit: protocol models, PushVerifier, RelayAPI, RelaySocket
  Shared/                compiled into the app and the notification extension: pairing in the
                         shared keychain, push → text, App Group inbox
  Agentring/             the app: Call/ (CallKit + LiveKit), Push/ (PushKit, notifications),
                         Model/, UI/ (SwiftUI), Resources/ (strings en/es, assets)
  NotificationService/   verifies each alert push and fills in the real text
  scripts/               test-core.sh, test-core-e2e.sh, render-icons.swift
```

## How pushes work on iOS

The relay talks to APNs directly (HTTP/2, token-based ES256 auth, `relay/src/push/apns.ts`).
An iPhone registers two tokens (`POST /v1/device/apns`):

| Event | Push | Why |
|---|---|---|
| `call.invite` | **VoIP** (PushKit) to `voip_token`, topic `<bundle>.voip`, expires with the ring | Wakes the app even if it was killed; it must report the call to CallKit at once |
| `msg.new` | alert, `mutable-content`, category `MSG_NEW` (Reply action) | Text only inside the signed event; the extension verifies `sig` and swaps it in |
| `call.missed` | alert, `mutable-content`, kept 24 h | The "missed call" the user reads later |
| `call.accept` (another phone joined) | passive alert, category `CALL_JOINABLE` (Join action), one per call | "Call in progress, tap to join" |
| `call.end` | silent (`content-available`, priority 5) | Clears the join offer |

Nothing but `call.invite` may use the VoIP token: iOS terminates apps that receive a VoIP
push without reporting a call, and after repeated violations stops delivering them. An
invite that can't ring (expired, bad signature, already on a call) is still reported and
ended at once (`CallController.reportAndEnd`).

Alert texts are `loc-key`s (`PUSH_MSG_NEW`, `PUSH_CALL_MISSED`, `PUSH_CALL_JOINABLE` in the
app's Localizable.strings): if the extension can't verify a push, the user sees a generic,
translated line and nothing unverified.

## Relay configuration

```
APNS_KEY_FILE=/secrets/apns-AuthKey.p8   # Apple developer account → Keys → + → Apple Push Notifications service
APNS_KEY_ID=ABC123DEFG                   # the key's id
APNS_TEAM_ID=TEAM123456                  # the team id (Membership)
APNS_TOPIC=dev.agentring.app             # bundle id (default)
```

One key serves sandbox (Xcode debug builds) and production (TestFlight, App Store); each
phone says which gateway its tokens belong to (`apns_env`). Hub mode shares one APNs client
across tenants, like FCM.

### Self-hosted relays

APNs only accepts pushes signed with the app publisher's key, so a self-hosted relay can't
wake the App Store app (on Android every user brings their own Firebase project, which iOS
doesn't allow). Today an iPhone paired with a self-hosted relay works while the app is open
(the pair response has `push_config: null` and the app says so). Planned: a push gateway on
agentring.io that self-hosted relays forward to, with the event encrypted for the phone so the
gateway can't read it. Building the app yourself with your own Apple team and APNs key also
works.

## Building

```bash
brew install xcodegen
cd app-ios
echo 'DEVELOPMENT_TEAM = <your team id>' > Config/Local.xcconfig
xcodegen
open Agentring.xcodeproj
```

The Apple developer account needs, for bundle ids `dev.agentring.app` and
`dev.agentring.app.NotificationService`: Push Notifications, App Groups
(`group.dev.agentring.app`) and Keychain Sharing. With automatic signing Xcode creates them.
CallKit and PushKit only work on a real iPhone, not the simulator.

## Tests

```bash
app-ios/scripts/test-core.sh       # AgentringCore unit tests (fixtures in protocol/examples, push signature vector)
app-ios/scripts/test-core-e2e.sh   # AgentringCore against a real relay (+ LiveKit if `livekit-server` is installed)
cd relay && npm test               # includes test/apns.test.ts
```

Both scripts run with only the Command Line Tools (no Xcode). The app itself needs Xcode.

```bash
app-ios/scripts/test-ui.sh         # XCUITest of the real app in the simulator against a local relay:
                                   # onboarding, pair by pasted code, socket, message in and reply out,
                                   # "agent offline", incoming call reported to CallKit
app-ios/scripts/dev-relay.sh       # a local relay (+ LiveKit) and a pairing code, to try the app by hand
```

The simulator has no CallKit incoming-call screen: it reports the call fine and then ends
it at once (a `CXEndCallAction` from the system ~0.1 s later). Ringing, answering and audio
need a real iPhone, as do VoIP pushes.

## App Store notes

- Background modes: `voip`, `audio`, `remote-notification`. Review asks for a demo: reviewer
  login on agentring cloud (REVIEW_LOGIN_* on the cloud) and a short video of a locked iPhone
  ringing and the call being answered.
- No sign-in inside the app (pairing is a QR code from the website), so "Sign in with Apple"
  doesn't apply. The app shows `agentring.io` as plain text and never links to plans or
  payments (guideline 3.1.1).
- `ITSAppUsesNonExemptEncryption = false`: only standard HTTPS/TLS and WebRTC.
