import { readFileSync } from "node:fs";
import { buildApp } from "./app.js";
import { loadConfig } from "./config.js";
import { buildHub } from "./hub/hub.js";

const cfg = loadConfig();

function shutdownOn(close: () => Promise<unknown>, log: { info: (o: object, m: string) => void }): void {
  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => {
      log.info({ signal }, "shutting down");
      void close().then(() => process.exit(0));
    });
  }
}

if (process.env.AGENTRING_HUB === "1") {
  // Hub mode (docs/cloud.md): many tenants under /t/{tenant_id}, managed through the admin API.
  const file = process.env.AGENTRING_HUB_ADMIN_TOKEN_FILE;
  const adminToken = (file ? readFileSync(file, "utf8") : (process.env.AGENTRING_HUB_ADMIN_TOKEN ?? "")).trim();
  if (!adminToken) throw new Error("Hub mode needs AGENTRING_HUB_ADMIN_TOKEN_FILE or AGENTRING_HUB_ADMIN_TOKEN");
  const { app, hub } = await buildHub(cfg, { adminToken });
  shutdownOn(() => app.close(), app.log);
  await app.listen({ host: cfg.host, port: cfg.port });
  app.log.info({ public_url: cfg.publicUrl, tenants: hub.registry.ids().length }, "agentring relay ready (hub mode)");
} else {
  const { app, ctx, apiKey, apiKeyGenerated } = await buildApp(cfg);

  if (apiKeyGenerated) {
    // Printed once so the user can configure the daemon. Also stored in DATA_DIR/api_key.
    console.log(`\n  agentring relay API key (configure the daemon with it):\n\n    ${apiKey}\n`);
  }
  shutdownOn(() => app.close(), app.log);
  await app.listen({ host: cfg.host, port: cfg.port });
  app.log.info(
    { relay_id: ctx.relayId, public_url: cfg.publicUrl, push: ctx.pushProviderName },
    "agentring relay ready",
  );
}
