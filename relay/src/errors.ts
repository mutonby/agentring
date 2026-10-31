export type ErrorCode =
  | "invalid_request"
  | "unauthorized"
  | "invalid_pairing_token"
  | "forbidden"
  | "not_found"
  | "busy"
  | "no_devices"
  | "agent_offline"
  | "calls_unavailable"
  | "invalid_state"
  | "call_ended"
  | "rate_limited"
  | "quota_exceeded"
  | "tenant_disabled"
  | "internal";

export class ApiError extends Error {
  constructor(
    readonly statusCode: number,
    readonly code: ErrorCode,
    message: string,
  ) {
    super(message);
  }
}

export const unauthorized = (msg = "Missing or invalid credentials") => new ApiError(401, "unauthorized", msg);
export const forbidden = (msg = "Not allowed") => new ApiError(403, "forbidden", msg);
export const notFound = (what: string) => new ApiError(404, "not_found", `${what} not found`);
export const quotaExceeded = (msg: string) => new ApiError(402, "quota_exceeded", msg);
