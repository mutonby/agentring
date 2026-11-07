import type { FastifyInstance } from "fastify";
import { partyOf } from "../auth.js";
import { type Ctx, principal, requireAuth } from "../context.js";
import { notFound } from "../errors.js";
import { newId, wellFormed } from "../ids.js";
import type { Message, Party } from "../protocol.js";
import { checkMessage } from "../quota.js";

interface MessageRow {
  msg_id: string;
  sender: Party;
  text: string;
  reply_to: string | null;
  client_msg_id: string | null;
  device_id: string | null;
  created_at: number;
  read_at: number | null;
}

function fromRow(r: MessageRow): Message {
  return {
    msg_id: r.msg_id,
    from: r.sender,
    text: r.text,
    ...(r.reply_to ? { reply_to: r.reply_to } : {}),
    ...(r.client_msg_id ? { client_msg_id: r.client_msg_id } : {}),
    ...(r.device_id ? { device_id: r.device_id } : {}),
    created_at: r.created_at,
    ...(r.read_at ? { read_at: r.read_at } : {}),
  };
}

const ID = { type: "string", maxLength: 64 } as const;

export function messageRoutes(app: FastifyInstance, ctx: Ctx): void {
  const { db, events } = ctx;
  const auth = requireAuth(ctx, "agent", "device");
  const get = (id: string) =>
    db.prepare("SELECT * FROM messages WHERE msg_id = ?").get(id) as unknown as MessageRow | undefined;

  app.post<{ Body: { text: string; reply_to?: string; client_msg_id?: string } }>(
    "/v1/messages",
    {
      preHandler: auth,
      schema: {
        body: {
          type: "object",
          additionalProperties: false,
          required: ["text"],
          properties: {
            text: { type: "string", minLength: 1, maxLength: 4000 },
            reply_to: ID,
            client_msg_id: ID,
          },
        },
      },
    },
    async (req, reply) => {
      const p = principal(req);
      const from = partyOf(p);
      const { client_msg_id } = req.body;
      const text = wellFormed(req.body.text);
      const reply_to = req.body.reply_to === undefined ? undefined : wellFormed(req.body.reply_to);

      if (client_msg_id) {
        const existing = db
          .prepare("SELECT * FROM messages WHERE sender = ? AND client_msg_id = ?")
          .get(from, client_msg_id) as unknown as MessageRow | undefined;
        if (existing) return reply.code(201).send(fromRow(existing));
      }
      checkMessage(db, ctx.limits());

      const row: MessageRow = {
        msg_id: newId("msg"),
        sender: from,
        text,
        reply_to: reply_to ?? null,
        client_msg_id: client_msg_id ?? null,
        device_id: p.kind === "device" ? p.deviceId : null,
        created_at: Date.now(),
        read_at: null,
      };
      db.prepare(
        `INSERT INTO messages (msg_id, sender, text, reply_to, client_msg_id, device_id, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`,
      ).run(row.msg_id, row.sender, row.text, row.reply_to, row.client_msg_id, row.device_id, row.created_at);
      events.emit({
        type: "msg.new",
        msg_id: row.msg_id,
        from,
        text,
        ...(reply_to ? { reply_to } : {}),
      });
      return reply.code(201).send(fromRow(row));
    },
  );

  app.get<{ Querystring: { after?: string; since?: number; from?: Party; limit?: number } }>(
    "/v1/messages",
    {
      preHandler: auth,
      schema: {
        querystring: {
          type: "object",
          properties: {
            after: ID,
            since: { type: "integer", minimum: 0 },
            from: { type: "string", enum: ["agent", "user"] },
            limit: { type: "integer", minimum: 1, maximum: 500, default: 100 },
          },
        },
      },
    },
    async (req) => {
      const { after, since, from, limit = 100 } = req.query;
      const where: string[] = [];
      const args: Array<string | number> = [];
      if (after) {
        where.push("msg_id > ?");
        args.push(after);
      }
      if (since !== undefined) {
        where.push("created_at >= ?");
        args.push(since);
      }
      if (from) {
        where.push("sender = ?");
        args.push(from);
      }
      const rows = db
        .prepare(
          `SELECT * FROM messages ${where.length ? "WHERE " + where.join(" AND ") : ""} ORDER BY msg_id LIMIT ?`,
        )
        .all(...args, limit + 1) as unknown as MessageRow[];
      return { messages: rows.slice(0, limit).map(fromRow), has_more: rows.length > limit };
    },
  );

  app.get<{ Params: { msg_id: string } }>("/v1/messages/:msg_id", { preHandler: auth }, async (req) => {
    const row = get(req.params.msg_id);
    if (!row) throw notFound("Message");
    return fromRow(row);
  });

  app.post<{ Params: { msg_id: string } }>("/v1/messages/:msg_id/read", { preHandler: auth }, async (req, reply) => {
    const by = partyOf(principal(req));
    const row = get(req.params.msg_id);
    // You can only mark the other party's messages as read.
    if (!row || row.sender === by) throw notFound("Message");
    const res = db
      .prepare("UPDATE messages SET read_at = ? WHERE msg_id = ? AND read_at IS NULL")
      .run(Date.now(), row.msg_id);
    if (res.changes === 1) events.emit({ type: "msg.read", msg_id: row.msg_id, by });
    return reply.code(204).send();
  });
}
