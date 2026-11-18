import Foundation
import Testing
@testable import AgentringCore

/// protocol/ at the repository root, found from this file's path.
let protocolDir: URL = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent()
    .appendingPathComponent("protocol")

func fixture(_ path: String) throws -> String {
    try String(contentsOf: protocolDir.appendingPathComponent(path), encoding: .utf8)
}

@Suite struct ProtocolFixturesTests {
    @Test func everyEventFixtureDecodes() throws {
        let dir = protocolDir.appendingPathComponent("examples/events")
        let files = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".json") }
        #expect(files.count >= 10)
        for f in files {
            let ev = try Event.parse(try fixture("examples/events/\(f)"))
            #expect(ev.type + ".json" == f || f.hasPrefix(ev.type), "\(f)")
            if case .unknown = ev.body { Issue.record("\(f) decoded as unknown") }
        }
    }

    @Test func callInviteFields() throws {
        let ev = try Event.parse(try fixture("examples/events/call.invite.json"))
        guard case .callInvite(let inv) = ev.body else { Issue.record("not an invite"); return }
        #expect(inv.agentName == "Claude")
        #expect(inv.isUrgent)
        #expect(inv.expiresAt == 1761200030000)
        #expect(ev.callId == "call_01JB0000000000000000000002")
        #expect(ev.seq == 41)
    }

    @Test func unknownEventTypesAreTolerated() throws {
        let ev = try Event.parse(#"{"type":"future.thing","id":"evt_1","ts":5,"seq":9,"x":1}"#)
        #expect(ev.body == .unknown)
        #expect(ev.seq == 9)
    }

    @Test func pairResponseKnowsAboutApns() throws {
        let json = #"{"device_id":"dev_1","device_jwt":"j","push_secret":"s","relay_id":"rly_1","agent_name":"A","push_config":{"provider":"apns","apns":{"topic":"dev.agentring.app"}}}"#
        let r = try ProtocolJSON.decoder.decode(PairResponse.self, from: Data(json.utf8))
        #expect(r.canPush)
        let fcm = #"{"device_id":"dev_1","device_jwt":"j","push_secret":"s","relay_id":"rly_1","agent_name":"A","push_config":null}"#
        #expect(try !ProtocolJSON.decoder.decode(PairResponse.self, from: Data(fcm.utf8)).canPush)
    }

    @Test func pairRequestEncodesIosFields() throws {
        let req = PairRequest(pairingToken: "pt_x", deviceName: "iPhone", appVersion: "0.1.0", apnsToken: "ab", voipToken: "cd", apnsEnv: .sandbox)
        let obj = try JSONSerialization.jsonObject(with: ProtocolJSON.encoder.encode(req)) as! [String: Any]
        #expect(obj["platform"] as? String == "ios")
        #expect(obj["voip_token"] as? String == "cd")
        #expect(obj["apns_env"] as? String == "sandbox")
    }
}

@Suite struct PushVerifierTests {
    @Test func matchesTheSharedTestVector() throws {
        let v = try JSONSerialization.jsonObject(with: Data(try fixture("examples/push/signature-vector.json").utf8)) as! [String: Any]
        let secret = v["push_secret"] as! String
        let data = v["data"] as! [String: String]
        #expect(PushVerifier.sign(pushSecret: secret, eventJSON: data["event"]!) == data["sig"])

        let ok = PushVerifier.verify(data, expectedRelayId: data["relay_id"]!, pushSecret: secret)
        guard case .ok(let ev) = ok, case .msgNew(let m) = ev.body else { Issue.record("not ok: \(ok)"); return }
        #expect(m.text == "Build finished ✅")

        #expect(PushVerifier.verify(data, expectedRelayId: "rly_other", pushSecret: secret) != ok)
        var tampered = data
        tampered["event"] = data["event"]!.replacingOccurrences(of: "Build", with: "Evil")
        #expect(PushVerifier.verify(tampered, expectedRelayId: data["relay_id"]!, pushSecret: secret) == .rejected("bad signature"))
        var unsigned = data
        unsigned["sig"] = nil
        #expect(PushVerifier.verify(unsigned, expectedRelayId: data["relay_id"]!, pushSecret: secret) == .rejected("missing signature"))
    }

    @Test func acceptsAnApnsPayloadWithAps() throws {
        let v = try JSONSerialization.jsonObject(with: Data(try fixture("examples/push/signature-vector.json").utf8)) as! [String: Any]
        var payload: [AnyHashable: Any] = v["data"] as! [String: String]
        payload["aps"] = ["alert": ["loc-key": "PUSH_MSG_NEW"]]
        let r = PushVerifier.verify(payload, expectedRelayId: "rly_01JB0000000000000000000010", pushSecret: v["push_secret"] as! String)
        if case .rejected(let why) = r { Issue.record("rejected: \(why)") }
    }
}

@Suite struct PairingCodeTests {
    @Test func parsesValidCodes() throws {
        let p = try QrPayload.parse(#"  {"v":1,"relay_url":"https://relay.example.com","pairing_token":"pt_abc"} "#)
        #expect(p.relayUrl == "https://relay.example.com")
        _ = try QrPayload.parse(#"{"v":1,"relay_url":"http://192.168.1.20:8080","pairing_token":"pt_abc"}"#)
        _ = try QrPayload.parse(#"{"v":1,"relay_url":"https://agentring.io/t/tnt_01JB00000000000000000000AA","pairing_token":"pt_abc"}"#)
    }

    @Test(arguments: [
        "hello",
        #"{"v":2,"relay_url":"https://x","pairing_token":"pt_abc"}"#,
        #"{"v":1,"relay_url":"http://relay.example.com","pairing_token":"pt_abc"}"#,
        #"{"v":1,"relay_url":"ftp://x","pairing_token":"pt_abc"}"#,
        #"{"v":1,"relay_url":"https://x","pairing_token":"nope"}"#,
    ])
    func rejectsBadCodes(_ raw: String) {
        #expect(throws: ProtocolError.self) { try QrPayload.parse(raw) }
    }

    @Test func urlPolicy() throws {
        #expect(RelayURLPolicy.isPrivateHost("10.0.0.5"))
        #expect(RelayURLPolicy.isPrivateHost("100.101.1.2"))
        #expect(RelayURLPolicy.isPrivateHost("mac.local"))
        #expect(!RelayURLPolicy.isPrivateHost("8.8.8.8"))
        #expect(!RelayURLPolicy.isPrivateHost("172.32.0.1"))
        try RelayURLPolicy.checkLiveKit("wss://agentring.io")
        #expect(throws: ProtocolError.self) { try RelayURLPolicy.checkLiveKit("ws://lk.example.com") }
    }
}

// MARK: - RelayAPI against a stubbed URLSession

final class StubProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) -> (Int, String))?
    nonisolated(unsafe) static var seen: [URLRequest] = []

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var req = request
        if req.httpBody == nil, let stream = req.httpBodyStream {
            stream.open()
            var data = Data()
            var buf = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let n = stream.read(&buf, maxLength: buf.count)
                if n <= 0 { break }
                data.append(buf, count: n)
            }
            stream.close()
            req.httpBody = data
        }
        StubProtocol.seen.append(req)
        let (status, body) = StubProtocol.handler?(req) ?? (500, "")
        let res = HTTPURLResponse(url: req.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: res, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@Suite(.serialized) struct RelayAPITests {
    func api(jwt: String? = "jwt_1") -> RelayAPI {
        let c = URLSessionConfiguration.ephemeral
        c.protocolClasses = [StubProtocol.self]
        StubProtocol.seen = []
        return RelayAPI(baseURL: "https://relay.test/t/tnt_1/", deviceJwt: jwt, session: URLSession(configuration: c))
    }

    @Test func pairPostsWithoutAuth() async throws {
        let a = api(jwt: nil)
        StubProtocol.handler = { _ in (201, #"{"device_id":"dev_1","device_jwt":"j","push_secret":"s","relay_id":"rly_1","agent_name":"A","push_config":{"provider":"apns","apns":{"topic":"t"}}}"#) }
        let r = try await a.pair(PairRequest(pairingToken: "pt_1", deviceName: "iPhone", appVersion: nil, apnsToken: nil, voipToken: "ab", apnsEnv: .production))
        #expect(r.deviceId == "dev_1")
        let req = StubProtocol.seen[0]
        #expect(req.url?.absoluteString == "https://relay.test/t/tnt_1/v1/pair")
        #expect(req.value(forHTTPHeaderField: "Authorization") == nil)
    }

    @Test func errorsCarryTheRelayCode() async throws {
        let a = api()
        StubProtocol.handler = { _ in (402, #"{"error":{"code":"quota_exceeded","message":"Call minutes used up"}}"#) }
        do {
            _ = try await a.createCall()
            Issue.record("expected an error")
        } catch let e as RelayError {
            #expect(e.isQuotaExceeded)
            #expect(e.message == "Call minutes used up")
        }
        StubProtocol.handler = { _ in (401, "") }
        do {
            _ = try await a.status()
        } catch let e as RelayError {
            #expect(e.isUnauthorized)
            #expect(e.code == "http_401")
        }
    }

    @Test func callActionsAndTokens() async throws {
        let a = api()
        StubProtocol.handler = { _ in (204, "") }
        try await a.updateApnsTokens(ApnsTokens(voipToken: "ab", apnsEnv: .sandbox))
        try await a.reportRinging("call_1")
        let body = try JSONSerialization.jsonObject(with: StubProtocol.seen[0].httpBody ?? Data()) as! [String: String]
        #expect(body == ["voip_token": "ab", "apns_env": "sandbox"])
        #expect(StubProtocol.seen[0].value(forHTTPHeaderField: "Authorization") == "Bearer jwt_1")
        #expect(StubProtocol.seen[1].url?.path == "/t/tnt_1/v1/calls/call_1/ringing")
        #expect(StubProtocol.seen[1].httpMethod == "POST")
    }

    @Test func webSocketURL() throws {
        let r = try api().webSocketRequest(afterSeq: 42)
        #expect(r.url?.absoluteString == "wss://relay.test/t/tnt_1/v1/ws?after_seq=42")
        let local = try RelayAPI(baseURL: "http://192.168.1.2:8080", deviceJwt: "j").webSocketRequest(afterSeq: 0)
        #expect(local.url?.scheme == "ws")
    }
}
