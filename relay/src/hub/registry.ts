import { sha256 } from "../auth.js";
import { type Db, openDatabase } from "../db/database.js";
import { randomToken } from "../ids.js";
import { type Limits, UNLIMITED } from "../quota.js";

/** One tenant as the hub stores it (hub.db). The API key itself is never stored. */
export interface TenantRecord {
  tenant_id: string;
  api_key_hash: Buffer;
  agent_name: string;
  limits: Limits;
  disabled: boolean;
  created_at: number;
}

interface Row {
  tenant_id: string;
  api_key_hash: string;
  agent_name: string;
  limits: string;
  disabled: number;
  created_at: number;
}

const MIGRATIONS = [
  `
  CREATE TABLE tenants (
    tenant_id TEXT PRIMARY KEY,
    api_key_hash TEXT NOT NULL UNIQUE,
    agent_name TEXT NOT NULL,
    limits TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
  );
  `,
];

const fromRow = (r: Row): TenantRecord => ({
  tenant_id: r.tenant_id,
  api_key_hash: Buffer.from(r.api_key_hash, "hex"),
  agent_name: r.agent_name,
  limits: { ...UNLIMITED, ...(JSON.parse(r.limits) as Partial<Limits>) },
  disabled: r.disabled !== 0,
  created_at: r.created_at,
});

/** The tenant registry: which tenants exist, their key hash, name, limits and state. */
export class Registry {
  readonly db: Db;

  constructor(path: string) {
    this.db = openDatabase(path, MIGRATIONS);
  }

  get(tenantId: string): TenantRecord | undefined {
    const r = this.db.prepare("SELECT * FROM tenants WHERE tenant_id = ?").get(tenantId) as unknown as Row | undefined;
    return r && fromRow(r);
  }

  byApiKey(apiKey: string): TenantRecord | undefined {
    const r = this.db
      .prepare("SELECT * FROM tenants WHERE api_key_hash = ?")
      .get(sha256(apiKey).toString("hex")) as unknown as Row | undefined;
    return r && fromRow(r);
  }

  create(tenantId: string, agentName: string, limits: Limits): { record: TenantRecord; apiKey: string } {
    const apiKey = randomToken("ak");
    const now = Date.now();
    this.db
      .prepare("INSERT INTO tenants (tenant_id, api_key_hash, agent_name, limits, created_at) VALUES (?, ?, ?, ?, ?)")
      .run(tenantId, sha256(apiKey).toString("hex"), agentName, JSON.stringify(limits), now);
    return { record: this.get(tenantId)!, apiKey };
  }

  update(tenantId: string, patch: { agent_name?: string; limits?: Limits; disabled?: boolean }): void {
    if (patch.agent_name !== undefined) {
      this.db.prepare("UPDATE tenants SET agent_name = ? WHERE tenant_id = ?").run(patch.agent_name, tenantId);
    }
    if (patch.limits !== undefined) {
      this.db.prepare("UPDATE tenants SET limits = ? WHERE tenant_id = ?").run(JSON.stringify(patch.limits), tenantId);
    }
    if (patch.disabled !== undefined) {
      this.db.prepare("UPDATE tenants SET disabled = ? WHERE tenant_id = ?").run(patch.disabled ? 1 : 0, tenantId);
    }
  }

  rotateKey(tenantId: string): string {
    const apiKey = randomToken("ak");
    this.db
      .prepare("UPDATE tenants SET api_key_hash = ? WHERE tenant_id = ?")
      .run(sha256(apiKey).toString("hex"), tenantId);
    return apiKey;
  }

  delete(tenantId: string): void {
    this.db.prepare("DELETE FROM tenants WHERE tenant_id = ?").run(tenantId);
  }

  ids(): string[] {
    return (this.db.prepare("SELECT tenant_id FROM tenants").all() as Array<{ tenant_id: string }>).map((r) => r.tenant_id);
  }

  close(): void {
    this.db.close();
  }
}
