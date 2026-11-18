#!/usr/bin/env bash
# AgentringCore against a real relay (built from ../relay) on localhost, no LiveKit
# unless LIVEKIT_* are set. Usage: app-ios/scripts/test-core-e2e.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
relay="$here/../../relay"
[ -f "$relay/dist/main.js" ] || (cd "$relay" && npm run -s build)
data="$(mktemp -d)"
port="${PORT:-18087}"
key="ak_e2e_$(openssl rand -hex 12)"
lk_env=()
if command -v livekit-server >/dev/null && [ -z "${LIVEKIT_API_KEY:-}" ]; then
  # Local LiveKit so the call flow (accept → join info) runs too.
  lk_secret="$(openssl rand -hex 24)"
  livekit-server --dev --bind 127.0.0.1 --port 17880 --keys "e2ekey: $lk_secret" >"$data/livekit.log" 2>&1 &
  lk_pid=$!
  lk_env=(LIVEKIT_API_KEY=e2ekey LIVEKIT_API_SECRET="$lk_secret" LIVEKIT_URL=ws://127.0.0.1:17880)
  for _ in $(seq 50); do curl -s -o /dev/null "http://127.0.0.1:17880" && break; sleep 0.1; done
fi
env ${lk_env[@]+"${lk_env[@]}"} PORT=$port AGENTRING_PUBLIC_URL="http://127.0.0.1:$port" AGENTRING_INSECURE_DEV=1 AGENTRING_DATA_DIR="$data" \
  AGENTRING_API_KEY="$key" AGENTRING_AGENT_NAME=E2E HOST=127.0.0.1 \
  node --disable-warning=ExperimentalWarning "$relay/dist/main.js" >"$data/relay.log" 2>&1 &
pid=$!
trap 'kill $pid ${lk_pid:-} 2>/dev/null; rm -rf "$data"' EXIT
for _ in $(seq 50); do curl -sf "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break; sleep 0.1; done
AGENTRING_E2E_RELAY="http://127.0.0.1:$port" AGENTRING_E2E_API_KEY="$key" "$here/test-core.sh" --filter RelayE2ETests "$@"
