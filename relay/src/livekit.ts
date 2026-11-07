import { AccessToken, RoomServiceClient, WebhookReceiver, type WebhookEvent } from "livekit-server-sdk";
import type { LiveKitConfig } from "./config.js";
import type { Schemas } from "./protocol.js";

/** Join tokens only need to outlive the join itself; an established session is not affected. */
export const TOKEN_TTL_S = 120;

export type JoinInfo = Schemas["JoinInfo"];

/** What the relay needs from LiveKit. Injectable so tests run without a LiveKit server. */
export interface LiveKitService {
  join(room: string, identity: string, name: string): Promise<JoinInfo>;
  /**
   * Creates the room. Deployments run with `room.auto_create: false`, so a join token
   * can't bring a finished call's room back. It closes after `emptyTimeoutS` with nobody in it.
   */
  createRoom(room: string, emptyTimeoutS?: number): Promise<void>;
  /** Disconnects everyone. Best effort: the room may already be gone. */
  deleteRoom(room: string): Promise<void>;
  /** Verifies and parses a webhook request; throws if the signature is wrong. */
  receiveWebhook(body: string, authorization: string | undefined): Promise<WebhookEvent>;
}

export class LiveKitServer implements LiveKitService {
  private readonly rooms: RoomServiceClient;
  private readonly receiver: WebhookReceiver;

  constructor(private readonly cfg: LiveKitConfig) {
    this.rooms = new RoomServiceClient(cfg.apiUrl, cfg.apiKey, cfg.apiSecret);
    this.receiver = new WebhookReceiver(cfg.apiKey, cfg.apiSecret);
  }

  async join(room: string, identity: string, name: string): Promise<JoinInfo> {
    const at = new AccessToken(this.cfg.apiKey, this.cfg.apiSecret, { identity, name, ttl: TOKEN_TTL_S });
    at.addGrant({ room, roomJoin: true, canPublish: true, canSubscribe: true, canPublishData: true });
    return {
      livekit_url: this.cfg.publicUrl,
      livekit_token: await at.toJwt(),
      room,
      identity,
      expires_at: Date.now() + TOKEN_TTL_S * 1000,
    };
  }

  async createRoom(room: string, emptyTimeoutS = 30): Promise<void> {
    // A short empty timeout so an abandoned call doesn't keep a room around.
    await this.rooms.createRoom({ name: room, emptyTimeout: emptyTimeoutS, departureTimeout: 10, maxParticipants: 4 });
  }

  async deleteRoom(room: string): Promise<void> {
    await this.rooms.deleteRoom(room);
  }

  receiveWebhook(body: string, authorization: string | undefined): Promise<WebhookEvent> {
    return this.receiver.receive(body, authorization);
  }
}
