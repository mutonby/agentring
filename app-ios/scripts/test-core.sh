#!/usr/bin/env bash
# Runs AgentringCore's tests on a Mac. With Xcode installed `swift test` is enough;
# with only the Command Line Tools, swift-testing lives outside the default search paths.
set -euo pipefail
cd "$(dirname "$0")/../AgentringCore"
if xcodebuild -version >/dev/null 2>&1; then
  exec swift test "$@"
fi
CLT=/Library/Developer/CommandLineTools/Library/Developer
exec swift test \
  -Xswiftc -F"$CLT/Frameworks" -Xlinker -F"$CLT/Frameworks" \
  -Xlinker -rpath -Xlinker "$CLT/Frameworks" -Xlinker -rpath -Xlinker "$CLT/usr/lib" "$@"
