import AgentringCore
import Foundation
import Observation
import os
import UIKit
import UserNotifications

private let log = Logger(subsystem: "dev.agentring.app", category: "app")

/// App-wide state: the pairing, the relay connection, messages and status.
@MainActor
@Observable
final class AppModel {
    static let shared = AppModel()

    enum Tab: Hashable { case home, messages, settings }
    enum Connection: Equatable { case connecting, connected, offline }
    enum PushState: Equatable { case unknown, registering, registered, notConfigured, error(String) }

    private(set) var pairing: Pairing?
    /// The relay no longer accepts this phone.
    private(set) var revoked = false
    /// Onboarding finished (pairing alone isn't enough: the permission and "try it" steps follow).
    private(set) var onboarded: Bool
    var selectedTab: Tab = .home
    var isShowingMessages = false

    private(set) var connection: Connection = .offline
    private(set) var agentOnline: Bool?
    var pushState: PushState = .unknown
    let messages = MessageStore()

    @ObservationIgnored private var socket: RelaySocket?
    private var lastSeq: Int64 {
        get { Int64(SharedConfig.defaults.integer(forKey: "events.last_seq")) }
        set { SharedConfig.defaults.set(Int(newValue), forKey: "events.last_seq") }
    }

    init() {
        pairing = PairingKeychain.load()
        onboarded = SharedConfig.defaults.bool(forKey: "onboarded") && PairingKeychain.load() != nil
    }

    var api: RelayAPI? {
        pairing.map { RelayAPI(baseURL: $0.relayUrl, deviceJwt: $0.deviceJwt) }
    }

    var agentName: String { pairing?.agentName ?? "Agent" }

    func finishOnboarding() {
        onboarded = true
        SharedConfig.defaults.set(true, forKey: "onboarded")
    }

    // MARK: - lifecycle

    func start() {
        if pairing != nil { pushState = pairing?.canPush == true ? .registering : .notConfigured }
        messages.load()
        messages.merge(SharedInbox.drain())
    }

    /// Foreground (or a call): keep a live socket and catch up.
    func becameActive() {
        guard let pairing, !revoked else { return }
        messages.merge(SharedInbox.drain())
        startSocket(pairing)
        Task { await refresh() }
        PushRegistrar.shared.uploadIfNeeded()
    }

    /// Background without a call: the socket would be cut anyway; push takes over.
    func enteredBackground() {
        guard CallController.shared.phase?.isLive != true else { return }
        socket?.stop()
    }

    func refresh() async {
        guard let api else { return }
        do {
            let status = try await api.status()
            agentOnline = status.agentOnline
            CallController.shared.onActiveCall(status.activeCall)
            let page = try await api.listMessages(limit: 200)
            messages.merge(page.messages)
        } catch let e as RelayError where e.isUnauthorized {
            onUnauthorized()
        } catch {
            log.info("refresh failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    private func startSocket(_ p: Pairing) {
        if socket == nil {
            let api = RelayAPI(baseURL: p.relayUrl, deviceJwt: p.deviceJwt)
            socket = RelaySocket(
                api: api,
                expectedRelayId: p.relayId,
                lastSeq: { Int64(SharedConfig.defaults.integer(forKey: "events.last_seq")) },
                handlers: .init(
                    onEvent: { e in Task { @MainActor in AppModel.shared.onEvent(e) } },
                    onState: { s in Task { @MainActor in AppModel.shared.onSocketState(s) } },
                    onUnauthorized: { Task { @MainActor in AppModel.shared.onUnauthorized() } },
                    onWrongRelay: { id in Task { @MainActor in log.error("socket reached another relay: \(id, privacy: .public)") } }
                )
            )
        }
        socket?.start()
        socket?.kick()
    }

    private func onSocketState(_ s: RelaySocket.State) {
        connection = switch s {
        case .connecting: .connecting
        case .connected: .connected
        case .disconnected: .offline
        }
    }

    func onEvent(_ e: Event) {
        if let seq = e.seq, seq > lastSeq { lastSeq = seq }
        switch e.body {
        case .msgNew(let m):
            messages.merge([m.toMessage(ts: e.ts)])
        case .msgRead(let r):
            messages.markRead(r.msgId, at: e.ts)
        case .agentPresence(let p):
            agentOnline = p.online
        case .callInvite(let invite):
            CallController.shared.reportIncoming(invite, source: .socket)
        case .wsHello:
            break
        default:
            CallController.shared.onEvent(e)
        }
    }

    func onUnauthorized() {
        guard pairing != nil else { return }
        log.warning("the relay rejected this phone's credentials")
        revoked = true
        socket?.stop()
    }

    // MARK: - pairing

    /// Pairs with a scanned or pasted code. Throws with a user-facing message.
    func pair(code: String) async throws {
        let qr = try QrPayload.parse(code)
        // Ask now: the relay can only ring a phone that allows notifications.
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
        let push = PushRegistrar.shared
        let req = PairRequest(
            pairingToken: qr.pairingToken,
            deviceName: UIDevice.current.name,
            appVersion: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String,
            apnsToken: push.apnsToken,
            voipToken: push.voipToken,
            apnsEnv: PushRegistrar.env
        )
        let res: PairResponse
        do {
            res = try await RelayAPI(baseURL: qr.relayUrl, deviceJwt: nil).pair(req)
        } catch let e as RelayError {
            throw ProtocolError(e.code == "invalid_pairing_token" ? L("pair_code_used") : e.message)
        }
        let p = Pairing(relayUrl: qr.relayUrl, deviceId: res.deviceId, deviceJwt: res.deviceJwt, pushSecret: res.pushSecret,
                        relayId: res.relayId, agentName: res.agentName, canPush: res.canPush)
        guard PairingKeychain.save(p) else { throw ProtocolError(L("pair_keychain_failed")) }
        socket?.stop()
        socket = nil
        lastSeq = 0
        messages.clear()
        pairing = p
        revoked = false
        pushState = p.canPush ? .registering : .notConfigured
        push.forgetUploadedTokens()
        push.uploadIfNeeded(force: true)
        if p.canPush, req.voipToken != nil { pushState = .registered }
        becameActive()
    }

    /// Forgets the pairing on this phone (the relay keeps it until revoked there).
    func unpair() {
        socket?.stop()
        socket = nil
        PairingKeychain.clear()
        PushRegistrar.shared.forgetUploadedTokens()
        lastSeq = 0
        messages.clear()
        pairing = nil
        revoked = false
        onboarded = false
        SharedConfig.defaults.set(false, forKey: "onboarded")
        agentOnline = nil
        pushState = .unknown
        selectedTab = .home
    }

    // MARK: - messages

    func send(_ text: String, replyTo: String? = nil) async -> String? {
        guard let api else { return L("call_agent_needs_relay") }
        let clientId = UUID().uuidString
        do {
            let m = try await api.postMessage(text, replyTo: replyTo, clientMsgId: clientId)
            messages.merge([m])
            return nil
        } catch let e as RelayError {
            if e.isUnauthorized { onUnauthorized() }
            return e.isQuotaExceeded ? L("message_quota") : String(format: L("send_failed"), e.message)
        } catch {
            return String(format: L("send_failed"), error.localizedDescription)
        }
    }

    /// From a notification's Reply action, possibly with the app in the background.
    func replyFromNotification(_ text: String, to msgId: String?) async {
        if let error = await send(text, replyTo: msgId) {
            let c = UNMutableNotificationContent()
            c.title = L("reply_failed_title")
            c.body = error
            try? await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
        } else if let msgId {
            try? await api?.markRead(msgId)
        }
    }

    func markAgentMessagesRead() {
        guard let api else { return }
        for m in messages.unreadFromAgent() {
            messages.markRead(m.msgId, at: Int64(Date().timeIntervalSince1970 * 1000))
            Task { try? await api.markRead(m.msgId) }
        }
    }

    func sendTestNotification() {
        let c = UNMutableNotificationContent()
        c.title = "agentring"
        c.body = L("test_notification_text")
        c.sound = .default
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }
}
