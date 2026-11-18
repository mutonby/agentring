#!/usr/bin/env bash
# UI test of the real app in the simulator against a local relay (+ LiveKit).
# Usage: app-ios/scripts/test-ui.sh [simulator name, default "iPhone 18 Pro"]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here/.."
sim="${1:-iPhone 18 Pro}"; shift || true
log="$(mktemp)"
key="ak_ui_$(openssl rand -hex 12)"
PORT=18091 AGENTRING_API_KEY="$key" "$here/dev-relay.sh" >"$log" 2>&1 &
relay_pid=$!
trap 'kill $relay_pid 2>/dev/null; pkill -P $relay_pid 2>/dev/null || true' EXIT
for _ in $(seq 100); do grep -q "pairing code:" "$log" && break; sleep 0.2; done
code="$(sed -n 's/^pairing code: //p' "$log")"
[ -n "$code" ] || { cat "$log"; exit 1; }
xcodegen generate >/dev/null
xcrun simctl uninstall "$sim" dev.agentring.app 2>/dev/null || true
TEST_RUNNER_AGENTRING_UI_RELAY="http://127.0.0.1:18091" TEST_RUNNER_AGENTRING_UI_API_KEY="$key" \
TEST_RUNNER_AGENTRING_UI_PAIRING_CODE="$code" \
  xcodebuild test -project Agentring.xcodeproj -scheme Agentring -destination "platform=iOS Simulator,name=$sim" \
  -derivedDataPath build/dd -resultBundlePath "build/ui-$(date +%s).xcresult" \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM= PROVISIONING_PROFILE_SPECIFIER= "$@" 2>&1 \
  | grep -E "Test Case|error:|failed|passed|\*\* TEST" || true
