#!/usr/bin/env bash
# UI test of the real app in the simulator against a local relay (+ LiveKit).
# Usage: app-ios/scripts/test-ui.sh [simulator name, default "iPhone 18 Pro"]
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cd "$here/.."
sim="${1:-iPhone 18 Pro}"; shift || true
log="$(mktemp)"
key="ak_ui_$(openssl rand -hex 12)"
relay_data="$(mktemp -d)"
DATA_DIR="$relay_data" PORT=18091 AGENTRING_API_KEY="$key" "$here/dev-relay.sh" >"$log" 2>&1 &
relay_pid=$!
trap 'kill $relay_pid 2>/dev/null; pkill -P $relay_pid 2>/dev/null || true' EXIT
for _ in $(seq 100); do grep -q "pairing code:" "$log" && break; sleep 0.2; done
code="$(sed -n 's/^pairing code: //p' "$log")"
[ -n "$code" ] || { cat "$log"; exit 1; }
xcodegen generate >/dev/null
xcrun simctl uninstall "$sim" dev.agentring.app 2>/dev/null || true
xcrun simctl boot "$sim" 2>/dev/null || true
xcrun simctl privacy "$sim" grant microphone dev.agentring.app 2>/dev/null || true

# The agent that answers "Call agent": Grok voice AI when a key is around (like production),
# otherwise the echo test agent. AGENTRING_UI_AGENT=none|echo|grok overrides.
agent="${AGENTRING_UI_AGENT:-}"
xai_file="${XAI_API_KEY_FILE:-$HOME/.claude/projects/-Users-mutonby-Documents-agentring/secrets/xai-api-key.txt}"
if [ -z "$agent" ]; then
  if [ -n "${XAI_API_KEY:-}" ] || [ -r "$xai_file" ]; then agent=grok; else agent=echo; fi
fi
agent_log="$(mktemp)"
repo="$here/../.."
case "$agent" in
  grok)
    agent_home="$(mktemp -d)"
    XAI_API_KEY="${XAI_API_KEY:-$(cat "$xai_file")}" AGENTRING_HOME="$agent_home" \
      AGENTRING_RELAY_URL="http://127.0.0.1:18091" AGENTRING_API_KEY="$key" \
      uv run --project "$repo/daemon" --extra voice agentring -v serve --ai --provider grok \
      --instructions "You are a test agent on a phone call. Greet the user in one short sentence." >"$agent_log" 2>&1 &
    agent_pid=$! ;;
  echo)
    uv run --project "$repo/daemon" --extra voice python "$here/fake-agent.py" "http://127.0.0.1:18091" "$key" >"$agent_log" 2>&1 &
    agent_pid=$! ;;
esac
trap 'kill ${agent_pid:-} $relay_pid 2>/dev/null; pkill -P $relay_pid 2>/dev/null; pkill -f "agentring.*serve --ai" 2>/dev/null; pkill -f fake-agent.py 2>/dev/null || true' EXIT
echo "agent: $agent (log $agent_log)"
sleep "${AGENT_WARMUP_S:-6}"
TEST_RUNNER_AGENTRING_UI_RELAY="http://127.0.0.1:18091" TEST_RUNNER_AGENTRING_UI_API_KEY="$key" \
TEST_RUNNER_AGENTRING_UI_PAIRING_CODE="$code" TEST_RUNNER_AGENTRING_UI_AGENT="$agent" \
  xcodebuild test -project Agentring.xcodeproj -scheme Agentring -destination "platform=iOS Simulator,name=$sim" \
  -derivedDataPath build/dd -resultBundlePath "build/ui-$(date +%s).xcresult" \
  CODE_SIGN_IDENTITY=- CODE_SIGN_STYLE=Manual DEVELOPMENT_TEAM= PROVISIONING_PROFILE_SPECIFIER= "$@" 2>&1 \
  | grep -E "Test Case|error:|failed|passed|\*\* TEST" || true
echo "--- agent log (last lines) ---"
grep -vE "DEBUG|msg.new text" "$agent_log" | tail -25
