import { readFileSync } from "node:fs";
import type { FastifyInstance } from "fastify";
import type { Ctx } from "../context.js";
import { notFound } from "../errors.js";

/** relay/web, next to src/ and dist/ (this file is in src/routes or dist/routes). */
const WEB_DIR = new URL("../../web/", import.meta.url);

/** Everything the browser client is made of. Nothing else under /web/ is served. */
const FILES: Record<string, string> = {
  "index.html": "text/html; charset=utf-8",
  "app.js": "text/javascript; charset=utf-8",
  "style.css": "text/css; charset=utf-8",
  // Copied from the pinned livekit-client package by `npm run build` (scripts/vendor-web.mjs).
  "vendor/livekit-client.esm.mjs": "text/javascript; charset=utf-8",
};

/** The origin of a URL, also as ws(s):// and http(s)://, for connect-src. */
function originsOf(url: string): string[] {
  const u = new URL(url);
  const secure = u.protocol === "https:" || u.protocol === "wss:";
  return [`${secure ? "https" : "http"}://${u.host}`, `${secure ? "wss" : "ws"}://${u.host}`];
}

/**
 * The browser client at /web/: pair this browser as a device, ring while the tab
 * is open, answer, call the agent and join calls. Same origin as the API, so no
 * CORS; the strict CSP only lets it talk to this relay and its LiveKit server.
 */
export function webRoutes(app: FastifyInstance, ctx: Ctx): void {
  const connect = new Set(["'self'", ...originsOf(ctx.cfg.publicUrl)]);
  if (ctx.cfg.livekit) for (const o of originsOf(ctx.cfg.livekit.publicUrl)) connect.add(o);
  const csp = [
    "default-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "media-src 'self' blob:",
    `connect-src ${[...connect].join(" ")}`,
    "base-uri 'none'",
    "form-action 'none'",
    "frame-ancestors 'none'",
  ].join("; ");

  const cache = new Map<string, Buffer>();
  const read = (name: string): Buffer | undefined => {
    let body = cache.get(name);
    if (!body) {
      try {
        body = readFileSync(new URL(name, WEB_DIR));
      } catch {
        return undefined; // e.g. vendor/ before the first `npm run build`
      }
      cache.set(name, body);
    }
    return body;
  };

  app.get("/web", async (_req, reply) => reply.redirect("/web/", 301));
  app.get<{ Params: { "*": string } }>("/web/*", async (req, reply) => {
    const name = req.params["*"] || "index.html";
    const type = FILES[name];
    const body = type ? read(name) : undefined;
    if (!type || !body) throw notFound("File");
    return reply
      .header("content-type", type)
      .header("content-security-policy", csp)
      .header("x-content-type-options", "nosniff")
      .header("referrer-policy", "no-referrer")
      .header("permissions-policy", "microphone=(self), camera=(), geolocation=()")
      .header("cache-control", "no-cache")
      .send(body);
  });
}
