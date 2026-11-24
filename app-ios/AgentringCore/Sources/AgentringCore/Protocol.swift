import Foundation

/// Swift mirror of protocol/relay.openapi.yaml. Kept honest by ProtocolFixturesTests,
/// which decodes every file in protocol/examples.

public enum ProtocolJSON {
    public static let decoder: JSONDecoder = JSONDecoder()
    public static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.outputFormatting = [.sortedKeys, .withoutEscapingSlashes]
        return e
    }()
}

public struct ProtocolError: LocalizedError, Equatable, Sendable {
    public let message: String
    public init(_ message: String) { self.message = message }
    public var errorDescription: String? { message }
}

public struct QrPayload: Codable, Equatable, Sendable {
    public let v: Int
    public let relayUrl: String
    public let pairingToken: String

    enum CodingKeys: String, CodingKey {
        case v
        case relayUrl = "relay_url"
        case pairingToken = "pairing_token"
    }

    /// Parses the QR / pasted pairing code. Throws `ProtocolError` with a user-facing reason.
    public static func parse(_ raw: String) throws -> QrPayload {
        let data = Data(raw.trimmingCharacters(in: .whitespacesAndNewlines).utf8)
        guard let p = try? ProtocolJSON.decoder.decode(QrPayload.self, from: data) else {
            throw ProtocolError("This is not an agentring pairing code")
        }
        guard p.v == 1 else { throw ProtocolError("Unsupported pairing code version \(p.v); update the app") }
        guard p.pairingToken.hasPrefix("pt_") else { throw ProtocolError("Invalid pairing token") }
        try RelayURLPolicy.check(p.relayUrl)
        return p
    }
}

public enum ApnsEnv: String, Codable, Sendable {
    case sandbox, production
}

public struct PairRequest: Codable, Sendable {
    public var pairingToken: String
    public var deviceName: String
    public var platform: String = "ios"
    public var appVersion: String?
    public var apnsToken: String?
    public var voipToken: String?
    public var apnsEnv: ApnsEnv?

    public init(pairingToken: String, deviceName: String, appVersion: String?, apnsToken: String?, voipToken: String?, apnsEnv: ApnsEnv?) {
        self.pairingToken = pairingToken
        self.deviceName = deviceName
        self.appVersion = appVersion
        self.apnsToken = apnsToken
        self.voipToken = voipToken
        self.apnsEnv = apnsEnv
    }

    enum CodingKeys: String, CodingKey {
        case pairingToken = "pairing_token"
        case deviceName = "device_name"
        case platform
        case appVersion = "app_version"
        case apnsToken = "apns_token"
        case voipToken = "voip_token"
        case apnsEnv = "apns_env"
    }
}

public struct ApnsTokens: Codable, Equatable, Sendable {
    public var apnsToken: String?
    public var voipToken: String?
    public var apnsEnv: ApnsEnv?

    public init(apnsToken: String? = nil, voipToken: String? = nil, apnsEnv: ApnsEnv? = nil) {
        self.apnsToken = apnsToken
        self.voipToken = voipToken
        self.apnsEnv = apnsEnv
    }

    enum CodingKeys: String, CodingKey {
        case apnsToken = "apns_token"
        case voipToken = "voip_token"
        case apnsEnv = "apns_env"
    }
}

/// `push_config` from the pair response. Only the provider matters on iOS.
public struct PushConfig: Codable, Equatable, Sendable {
    public let provider: String
}

public struct PairResponse: Codable, Equatable, Sendable {
    public let deviceId: String
    public let deviceJwt: String
    public let pushSecret: String
    public let relayId: String
    public let agentName: String
    public let pushConfig: PushConfig?

    enum CodingKeys: String, CodingKey {
        case deviceId = "device_id"
        case deviceJwt = "device_jwt"
        case pushSecret = "push_secret"
        case relayId = "relay_id"
        case agentName = "agent_name"
        case pushConfig = "push_config"
    }

    /// The relay can wake this iPhone (APNs is set up on it).
    public var canPush: Bool { pushConfig?.provider == "apns" }
}

public struct Message: Codable, Equatable, Identifiable, Sendable {
    public static let fromAgent = "agent"
    public static let fromUser = "user"

    public let msgId: String
    public let from: String
    public let text: String
    public var replyTo: String?
    public var clientMsgId: String?
    public var deviceId: String?
    public let createdAt: Int64
    public var readAt: Int64?
    public var truncated: Bool?

    public var id: String { msgId }
    public var isFromAgent: Bool { from == Message.fromAgent }

    public init(msgId: String, from: String, text: String, replyTo: String? = nil, clientMsgId: String? = nil, deviceId: String? = nil, createdAt: Int64, readAt: Int64? = nil, truncated: Bool? = nil) {
        self.msgId = msgId
        self.from = from
        self.text = text
        self.replyTo = replyTo
        self.clientMsgId = clientMsgId
        self.deviceId = deviceId
        self.createdAt = createdAt
        self.readAt = readAt
        self.truncated = truncated
    }

    enum CodingKeys: String, CodingKey {
        case msgId = "msg_id"
        case from, text
        case replyTo = "reply_to"
        case clientMsgId = "client_msg_id"
        case deviceId = "device_id"
        case createdAt = "created_at"
        case readAt = "read_at"
        case truncated
    }
}

public struct MessagePage: Codable, Sendable {
    public let messages: [Message]
    public let hasMore: Bool
    enum CodingKeys: String, CodingKey {
        case messages
        case hasMore = "has_more"
    }
}

public struct Call: Codable, Equatable, Sendable {
    public let callId: String
    public let direction: String
    public let state: String
    public var reason: String?
    public let priority: String
    public let agentName: String
    public let createdAt: Int64
    public var answeredAt: Int64?
    public var endedAt: Int64?
    public var endedBy: String?
    public var endReason: String?
    public var durationMs: Int64?
    public var deviceId: String?

    public var isRinging: Bool { state == "ringing" }
    public var isActive: Bool { state == "active" }
    /// A group call from the agent that is in progress: a phone not on it may join.
    public var isJoinable: Bool { isActive && direction == "agent_to_user" }

    enum CodingKeys: String, CodingKey {
        case callId = "call_id"
        case direction, state, reason, priority
        case agentName = "agent_name"
        case createdAt = "created_at"
        case answeredAt = "answered_at"
        case endedAt = "ended_at"
        case endedBy = "ended_by"
        case endReason = "end_reason"
        case durationMs = "duration_ms"
        case deviceId = "device_id"
    }
}

public struct Status: Codable, Sendable {
    public let relayId: String
    public let version: String
    public let agentName: String
    public let agentOnline: Bool
    public let pushProvider: String
    /// The call currently ringing or active, if any.
    public var activeCall: Call?

    enum CodingKeys: String, CodingKey {
        case relayId = "relay_id"
        case version
        case agentName = "agent_name"
        case agentOnline = "agent_online"
        case pushProvider = "push_provider"
        case activeCall = "active_call"
    }
}

public struct JoinInfo: Codable, Equatable, Sendable {
    public let livekitUrl: String
    public let livekitToken: String
    public let room: String
    public let identity: String
    public let expiresAt: Int64

    enum CodingKeys: String, CodingKey {
        case livekitUrl = "livekit_url"
        case livekitToken = "livekit_token"
        case room, identity
        case expiresAt = "expires_at"
    }
}

public struct CallWithJoin: Codable, Sendable {
    public let call: Call
    public let join: JoinInfo
}

public struct ApiErrorBody: Codable, Sendable {
    public struct Detail: Codable, Sendable {
        public let code: String
        public let message: String
    }
    public let error: Detail
}

// MARK: - Events

public struct CallInvite: Codable, Equatable, Sendable {
    public let callId: String
    public let direction: String
    public let agentName: String
    public let reason: String
    public let priority: String
    public let expiresAt: Int64
    public var isUrgent: Bool { priority == "urgent" }
    enum CodingKeys: String, CodingKey {
        case callId = "call_id"
        case direction
        case agentName = "agent_name"
        case reason, priority
        case expiresAt = "expires_at"
    }
}

public struct CallRinging: Codable, Equatable, Sendable {
    public let callId: String
    public let deviceId: String
    enum CodingKeys: String, CodingKey {
        case callId = "call_id"
        case deviceId = "device_id"
    }
}

/// call.accept and call.decline share a shape.
public struct CallAnswer: Codable, Equatable, Sendable {
    public let callId: String
    public let by: String
    public var deviceId: String?
    enum CodingKeys: String, CodingKey {
        case callId = "call_id"
        case by
        case deviceId = "device_id"
    }
}

public struct CallEnd: Codable, Equatable, Sendable {
    public let callId: String
    public let by: String
    public let reason: String
    public var durationMs: Int64?
    enum CodingKeys: String, CodingKey {
        case callId = "call_id"
        case by, reason
        case durationMs = "duration_ms"
    }
}

public struct CallMissed: Codable, Equatable, Sendable {
    public let callId: String
    enum CodingKeys: String, CodingKey { case callId = "call_id" }
}

public struct MsgNew: Codable, Equatable, Sendable {
    public let msgId: String
    public let from: String
    public let text: String
    public var replyTo: String?
    public var truncated: Bool?
    enum CodingKeys: String, CodingKey {
        case msgId = "msg_id"
        case from, text
        case replyTo = "reply_to"
        case truncated
    }
}

public struct MsgRead: Codable, Equatable, Sendable {
    public let msgId: String
    public let by: String
    enum CodingKeys: String, CodingKey {
        case msgId = "msg_id"
        case by
    }
}

public struct AgentPresence: Codable, Equatable, Sendable {
    public let online: Bool
}

public struct WsHello: Codable, Equatable, Sendable {
    public let relayId: String
    public let lastSeq: Int64
    public let serverTs: Int64
    enum CodingKeys: String, CodingKey {
        case relayId = "relay_id"
        case lastSeq = "last_seq"
        case serverTs = "server_ts"
    }
}

/// A relay event. Unknown types decode to `.unknown` so old apps keep working.
public struct Event: Decodable, Equatable, Sendable {
    public enum Body: Equatable, Sendable {
        case callInvite(CallInvite)
        case callRinging(CallRinging)
        case callAccept(CallAnswer)
        case callDecline(CallAnswer)
        case callEnd(CallEnd)
        case callMissed(CallMissed)
        case msgNew(MsgNew)
        case msgRead(MsgRead)
        case agentPresence(AgentPresence)
        case wsHello(WsHello)
        case unknown
    }

    public let type: String
    public let id: String
    public let ts: Int64
    public let seq: Int64?
    public let body: Body

    enum CodingKeys: String, CodingKey { case type, id, ts, seq }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        type = try c.decode(String.self, forKey: .type)
        id = (try? c.decode(String.self, forKey: .id)) ?? ""
        ts = (try? c.decode(Int64.self, forKey: .ts)) ?? 0
        seq = try? c.decodeIfPresent(Int64.self, forKey: .seq)
        switch type {
        case "call.invite": body = .callInvite(try CallInvite(from: decoder))
        case "call.ringing": body = .callRinging(try CallRinging(from: decoder))
        case "call.accept": body = .callAccept(try CallAnswer(from: decoder))
        case "call.decline": body = .callDecline(try CallAnswer(from: decoder))
        case "call.end": body = .callEnd(try CallEnd(from: decoder))
        case "call.missed": body = .callMissed(try CallMissed(from: decoder))
        case "msg.new": body = .msgNew(try MsgNew(from: decoder))
        case "msg.read": body = .msgRead(try MsgRead(from: decoder))
        case "agent.presence": body = .agentPresence(try AgentPresence(from: decoder))
        case "ws.hello": body = .wsHello(try WsHello(from: decoder))
        default: body = .unknown
        }
    }

    public static func parse(_ json: String) throws -> Event {
        try ProtocolJSON.decoder.decode(Event.self, from: Data(json.utf8))
    }

    /// The call this event is about, if any.
    public var callId: String? {
        switch body {
        case .callInvite(let e): e.callId
        case .callRinging(let e): e.callId
        case .callAccept(let e), .callDecline(let e): e.callId
        case .callEnd(let e): e.callId
        case .callMissed(let e): e.callId
        default: nil
        }
    }
}

extension MsgNew {
    public func toMessage(ts: Int64) -> Message {
        Message(msgId: msgId, from: from, text: text, replyTo: replyTo, createdAt: ts, truncated: truncated)
    }
}
