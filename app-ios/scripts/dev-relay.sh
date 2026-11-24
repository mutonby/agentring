#!/usr/bin/env bash
# A local relay (+ LiveKit if installed) for trying the app in the simulator.
# Prints a pairing code to paste in the app and the agent API key; Ctrl-C stops it.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
relay="$here/../../relay"
(cd "$relay" && npm run -s build)
data="${DATA_DIR:-$(mktemp -d)}"
port="${PORT:-18090}"
key="${AGENTRING_API_KEY:-ak_dev_$(openssl rand -hex 12)}"
pids=()
trap 'kill ${pids[@]+"${pids[@]}"} 2>/dev/null' EXIT
lk_env=()
if command -v livekit-server >/dev/null; then
  lk_secret="$(openssl rand -hex 24)"
  livekit-server --dev --bind 127.0.0.1 --port 17890 --keys "devkey: $lk_secret" >"$data/livekit.log" 2>&1 &
  pids+=($!)
  lk_env=(LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET="$lk_secret" LIVEKIT_URL=ws://127.0.0.1:17890)
fi
env ${lk_env[@]+"${lk_env[@]}"} PORT=$port AGENTRING_PUBLIC_URL="http://127.0.0.1:$port" AGENTRING_INSECURE_DEV=1 \
  AGENTRING_DATA_DIR="$data" AGENTRING_API_KEY="$key" AGENTRING_AGENT_NAME="${AGENT_NAME:-Claude}" HOST=127.0.0.1 \
  node --disable-warning=ExperimentalWarning "$relay/dist/main.js" >"$data/relay.log" 2>&1 &
pids+=($!)
for _ in $(seq 50); do curl -sf "http://127.0.0.1:$port/healthz" >/dev/null 2>&1 && break; sleep 0.1; done
code="$(curl -sf -X POST -H "Authorization: Bearer $key" "http://127.0.0.1:$port/v1/pairing-tokens" | python3 -c 'import sys,json;print(json.load(sys.stdin)["qr_payload"])')"
echo "relay:        http://127.0.0.1:$port  (logs in $data)"
echo "agent key:    $key"
echo "pairing code: $code"
wait
