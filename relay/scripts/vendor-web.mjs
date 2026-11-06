// Copies the pinned LiveKit browser SDK (a devDependency) into web/vendor/ so the
// relay serves it itself: no third-party CDN, and the version is locked in package-lock.json.
import { copyFileSync, mkdirSync } from "node:fs";

const src = new URL("../node_modules/livekit-client/dist/livekit-client.esm.mjs", import.meta.url);
const dir = new URL("../web/vendor/", import.meta.url);
mkdirSync(dir, { recursive: true });
copyFileSync(src, new URL("livekit-client.esm.mjs", dir));
