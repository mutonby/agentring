import Foundation

public struct RelayError: LocalizedError, Equatable, Sendable {
    public let status: Int
    public let code: String
    public let message: String

    public init(status: Int, code: String, message: String) {
        self.status = status
        self.code = code
        self.message = message
    }

    /// Credentials are no longer valid (device revoked or relay reset): the app must re-pair.
    public var isUnauthorized: Bool { status == 401 }
    /// A plan limit (agentring cloud): calls or messages used up.
    public var isQuotaExceeded: Bool { status == 402 || code == "quota_exceeded" }
    public var errorDescription: String? { message }
}

/// Async client for the relay REST API, as a device.
public struct RelayAPI: Sendable {
    public let baseURL: String
    private let deviceJwt: String?
    private let session: URLSession
    private let userAgent: String

    public static let defaultSession: URLSession = {
        let c = URLSessionConfiguration.default
        c.timeoutIntervalForRequest = 20
        c.waitsForConnectivity = false
        return URLSession(configuration: c)
    }()

    public init(baseURL: String, deviceJwt: String?, session: URLSession = RelayAPI.defaultSession, userAgent: String = "agentring-ios") {
        self.baseURL = baseURL.hasSuffix("/") ? String(baseURL.dropLast()) : baseURL
        self.deviceJwt = deviceJwt
        self.session = session
        self.userAgent = userAgent
    }

    private struct PostMessageBody: Encodable {
        let text: String
        let replyTo: String?
        let clientMsgId: String?
        enum CodingKeys: String, CodingKey {
            case text
            case replyTo = "reply_to"
            case clientMsgId = "client_msg_id"
        }
    }

    private struct CreateCallBody: Encodable { let reason: String? }

    public func pair(_ req: PairRequest) async throws -> PairResponse {
        try await call("POST", "/v1/pair", body: req, auth: false)
    }

    public func updateApnsTokens(_ tokens: ApnsTokens) async throws {
        _ = try await send("POST", "/v1/device/apns", body: tokens)
    }

    public func status() async throws -> Status { try await call("GET", "/v1/status") }

    public func listMessages(after: String? = nil, limit: Int = 200) async throws -> MessagePage {
        var path = "/v1/messages?limit=\(limit)"
        if let after { path += "&after=\(after)" }
        return try await call("GET", path)
    }

    public func getMessage(_ msgId: String) async throws -> Message { try await call("GET", "/v1/messages/\(msgId)") }

    public func postMessage(_ text: String, replyTo: String? = nil, clientMsgId: String? = nil) async throws -> Message {
        try await call("POST", "/v1/messages", body: PostMessageBody(text: text, replyTo: replyTo, clientMsgId: clientMsgId))
    }

    public func markRead(_ msgId: String) async throws {
        _ = try await send("POST", "/v1/messages/\(msgId)/read")
    }

    /// Calls the agent (`user_to_agent`). The relay returns this phone's LiveKit join
    /// info right away; the call rings until the agent accepts. 503 `agent_offline` /
    /// `calls_unavailable`, 409 `busy`, 402 `quota_exceeded`.
    public func createCall(reason: String? = nil) async throws -> CallWithJoin {
        try await call("POST", "/v1/calls", body: CreateCallBody(reason: reason))
    }

    public func getCall(_ callId: String) async throws -> Call { try await call("GET", "/v1/calls/\(callId)") }

    /// Tells the relay this phone is showing the incoming call (measures ring latency).
    public func reportRinging(_ callId: String) async throws {
        _ = try await send("POST", "/v1/calls/\(callId)/ringing")
    }

    /// Answers a ringing call; after the ring window it fails with 409 `invalid_state` (use `joinCall`).
    public func acceptCall(_ callId: String) async throws -> CallWithJoin {
        try await call("POST", "/v1/calls/\(callId)/accept")
    }

    /// Joins an agent call that is ringing or active. 409 `call_ended` once it is over.
    public func joinCall(_ callId: String) async throws -> CallWithJoin {
        try await call("POST", "/v1/calls/\(callId)/join")
    }

    @discardableResult
    public func declineCall(_ callId: String) async throws -> Call {
        try await call("POST", "/v1/calls/\(callId)/decline")
    }

    @discardableResult
    public func endCall(_ callId: String) async throws -> Call {
        try await call("POST", "/v1/calls/\(callId)/end")
    }

    public func webSocketRequest(afterSeq: Int64) throws -> URLRequest {
        guard var u = URLComponents(string: baseURL + "/v1/ws") else { throw ProtocolError("Invalid relay URL") }
        u.scheme = u.scheme == "http" ? "ws" : "wss"
        u.queryItems = [URLQueryItem(name: "after_seq", value: String(afterSeq))]
        guard let url = u.url, let jwt = deviceJwt else { throw ProtocolError("not paired") }
        var r = URLRequest(url: url)
        r.setValue("Bearer \(jwt)", forHTTPHeaderField: "Authorization")
        r.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        return r
    }

    // MARK: - plumbing

    private func call<T: Decodable>(_ method: String, _ path: String, auth: Bool = true) async throws -> T {
        try ProtocolJSON.decoder.decode(T.self, from: await send(method, path, body: nil as String?, auth: auth))
    }

    private func call<B: Encodable, T: Decodable>(_ method: String, _ path: String, body: B, auth: Bool = true) async throws -> T {
        try ProtocolJSON.decoder.decode(T.self, from: await send(method, path, body: body, auth: auth))
    }

    /// POSTs without a body still send an empty one (the relay expects Content-Length).
    @discardableResult
    private func send(_ method: String, _ path: String) async throws -> Data {
        try await send(method, path, body: nil as String?)
    }

    private func send<B: Encodable>(_ method: String, _ path: String, body: B?, auth: Bool = true) async throws -> Data {
        guard let url = URL(string: baseURL + path) else { throw ProtocolError("Invalid relay URL") }
        var r = URLRequest(url: url)
        r.httpMethod = method
        r.setValue(userAgent, forHTTPHeaderField: "User-Agent")
        if auth {
            guard let jwt = deviceJwt else { throw ProtocolError("not paired") }
            r.setValue("Bearer \(jwt)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try ProtocolJSON.encoder.encode(body)
        } else if method != "GET" {
            r.httpBody = Data()
        }
        let (data, response) = try await session.data(for: r)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let err = try? ProtocolJSON.decoder.decode(ApiErrorBody.self, from: data).error
            throw RelayError(status: status, code: err?.code ?? "http_\(status)", message: err?.message ?? "HTTP \(status)")
        }
        return data
    }
}
