import Foundation
import Testing
@testable import AgentringCore

/// Talks to a real relay. scripts/test-core-e2e.sh starts one and sets these variables;
/// without them the suite is skipped.
let e2eURL = ProcessInfo.processInfo.environment["AGENTRING_E2E_RELAY"]
let e2eKey = ProcessInfo.processInfo.environment["AGENTRING_E2E_API_KEY"] ?? ""

private func agent(_ method: String, _ path: String, _ body: [String: Any]? = nil) async throws -> [String: Any] {
    var r = URLRequest(url: URL(string: e2eURL! + path)!)
    r.httpMethod = method
    r.setValue("Bearer \(e2eKey)", forHTTPHeaderField: "Authorization")
    if let body {
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        r.httpBody = try JSONSerialization.data(withJSONObject: body)
    }
    let (data, _) = try await URLSession.shared.data(for: r)
    return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
}

private final class Inbox: @unchecked Sendable {
    private let lock = NSLock()
    private var events: [Event] = []
    private var lastSeq: Int64 = 0
    func add(_ e: Event) { lock.withLock { events.append(e); if let s = e.seq { lastSeq = max(lastSeq, s) } } }
    var seq: Int64 { lock.withLock { lastSeq } }
    func first(_ type: String) -> Event? { lock.withLock { events.first { $0.type == type } } }
    func wait(_ type: String, timeout: Double = 10) async -> Event? {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            if let e = first(type) { return e }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return nil
    }
}

@Suite(.enabled(if: e2eURL != nil), .serialized) struct RelayE2ETests {
    @Test func pairAsIphoneThenMessagesAndCallsOverTheSocket() async throws {
        let token = try await agent("POST", "/v1/pairing-tokens")
        let qr = try QrPayload.parse(token["qr_payload"] as! String)
        let anon = RelayAPI(baseURL: qr.relayUrl, deviceJwt: nil)
        let paired = try await anon.pair(PairRequest(pairingToken: qr.pairingToken, deviceName: "e2e iPhone", appVersion: "0.0.0", apnsToken: nil, voipToken: nil, apnsEnv: .sandbox))
        let api = RelayAPI(baseURL: qr.relayUrl, deviceJwt: paired.deviceJwt)
        try await api.updateApnsTokens(ApnsTokens(apnsToken: String(repeating: "b", count: 64), voipToken: String(repeating: "a", count: 64), apnsEnv: .sandbox))

        let devices = try await agent("GET", "/v1/devices")
        let me = (devices["devices"] as! [[String: Any]]).first { $0["device_id"] as? String == paired.deviceId }!
        #expect(me["platform"] as? String == "ios")
        #expect(me["has_push_token"] as? Bool == true)

        let inbox = Inbox()
        let socket = RelaySocket(api: api, expectedRelayId: paired.relayId, lastSeq: { inbox.seq }, handlers: .init(onEvent: { inbox.add($0) }))
        socket.start()
        defer { socket.stop() }
        #expect(await inbox.wait("ws.hello") != nil)

        _ = try await agent("POST", "/v1/messages", ["text": "hola iPhone"])
        let msg = await inbox.wait("msg.new")
        guard case .msgNew(let m)? = msg?.body else { Issue.record("no msg.new"); return }
        #expect(m.text == "hola iPhone")
        try await api.markRead(m.msgId)
        let reply = try await api.postMessage("recibido", replyTo: m.msgId, clientMsgId: "c-1")
        #expect(reply.from == Message.fromUser)

        let placed = try await agent("POST", "/v1/calls", ["reason": "e2e"])
        let callId = (placed["call"] as? [String: Any])?["call_id"] as? String
        guard case .callInvite(let inv)? = await inbox.wait("call.invite")?.body else {
            Issue.record("no call.invite (calls need LiveKit; placed: \(placed))")
            return
        }
        #expect(inv.callId == callId)
        try await api.reportRinging(inv.callId)
        let accepted = try await api.acceptCall(inv.callId)
        #expect(accepted.call.state == "active")
        try RelayURLPolicy.checkLiveKit(accepted.join.livekitUrl)
        let ended = try await api.endCall(inv.callId)
        #expect(ended.state == "ended")
        #expect(await inbox.wait("call.end") != nil)
    }

    @Test func wrongRelayIsDropped() async throws {
        let token = try await agent("POST", "/v1/pairing-tokens")
        let qr = try QrPayload.parse(token["qr_payload"] as! String)
        let paired = try await RelayAPI(baseURL: qr.relayUrl, deviceJwt: nil).pair(PairRequest(pairingToken: qr.pairingToken, deviceName: "e2e", appVersion: nil, apnsToken: nil, voipToken: nil, apnsEnv: nil))
        let wrong = Inbox()
        let flag = Inbox()
        let s = RelaySocket(api: RelayAPI(baseURL: qr.relayUrl, deviceJwt: paired.deviceJwt), expectedRelayId: "rly_someone_else", lastSeq: { 0 },
                            handlers: .init(onEvent: { wrong.add($0) }, onWrongRelay: { _ in flag.add(try! Event.parse(#"{"type":"flag","id":"x","ts":0}"#)) }))
        s.start()
        #expect(await flag.wait("flag") != nil)
        #expect(wrong.first("ws.hello") == nil)
        s.stop()
    }

    @Test func revokedDeviceGetsUnauthorized() async throws {
        let token = try await agent("POST", "/v1/pairing-tokens")
        let qr = try QrPayload.parse(token["qr_payload"] as! String)
        let paired = try await RelayAPI(baseURL: qr.relayUrl, deviceJwt: nil).pair(PairRequest(pairingToken: qr.pairingToken, deviceName: "e2e", appVersion: nil, apnsToken: nil, voipToken: nil, apnsEnv: nil))
        _ = try await agent("DELETE", "/v1/devices/\(paired.deviceId)")
        do {
            _ = try await RelayAPI(baseURL: qr.relayUrl, deviceJwt: paired.deviceJwt).status()
            Issue.record("expected 401")
        } catch let e as RelayError {
            #expect(e.isUnauthorized)
        }
    }
}
