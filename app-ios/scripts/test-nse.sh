#!/usr/bin/env bash
# The notification service extension's real code on macOS (simulated pushes skip extensions).
set -euo pipefail
cd "$(dirname "$0")/.."
(cd AgentringCore && swift build -q)
bin="$(cd AgentringCore && swift build --show-bin-path)"
out="$(mktemp -d)"
# SwiftPM puts the module in Modules/ (older toolchains) or next to the library (newer ones).
mods="$bin/Modules"; [ -d "$mods" ] || mods="$bin"
lib="$bin/libAgentringCore.a"; [ -f "$lib" ] || lib="$(ls "$bin"/AgentringCore.build/*.o)"
swiftc -I "$mods" -module-name NSETests -o "$out/nse-tests" \
  Shared/*.swift NotificationService/NotificationService.swift NotificationServiceTests/main.swift $lib
"$out/nse-tests"
