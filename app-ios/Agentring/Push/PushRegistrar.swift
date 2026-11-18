import AgentringCore
import Foundation
import os
import PushKit
import UIKit
import UserNotifications

private let log = Logger(subsystem: "dev.agentring.app", category: "push")

/// PushKit (VoIP, call invites) and regular APNs (messages, missed calls, "join").
/// Created at launch: iOS only delivers a VoIP push to an app that registers for it
/// again every time it starts, including when it is launched in the background by one.
@MainActor
final class PushRegistrar: NSObject {
    static let shared = PushRegistrar()

    private var voip: PKPushRegistry?
    private(set) var voipToken: String?
    private(set) var apnsToken: String?
    /// Xcode debug builds get sandbox tokens; TestFlight and the App Store, production ones.
    static let env: ApnsEnv = {
        #if DEBUG
        return .sandbox
        #else
        return .production
        #endif
    }()

    private let sentKey = "push.sent_tokens"

    func start() {
        let r = PKPushRegistry(queue: .main)
        r.delegate = self
        r.desiredPushTypes = [.voIP]
        voip = r
        UNUserNotificationCenter.current().delegate = NotificationRouter.shared
        NotificationRouter.shared.registerCategories()
        UIApplication.shared.registerForRemoteNotifications()
    }

    func didRegister(apnsToken data: Data) {
        apnsToken = data.map { String(format: "%02x", $0) }.joined()
        uploadIfNeeded()
    }

    /// Sends the tokens to the relay when they changed since the last upload.
    func uploadIfNeeded(force: Bool = false) {
        guard let pairing = PairingKeychain.load() else { return }
        let tokens = ApnsTokens(apnsToken: apnsToken, voipToken: voipToken, apnsEnv: Self.env)
        guard tokens.apnsToken != nil || tokens.voipToken != nil else { return }
        let fingerprint = "\(pairing.deviceId)|\(tokens.apnsToken ?? "")|\(tokens.voipToken ?? "")|\(Self.env.rawValue)"
        if !force, SharedConfig.defaults.string(forKey: sentKey) == fingerprint { return }
        Task {
            do {
                try await RelayAPI(baseURL: pairing.relayUrl, deviceJwt: pairing.deviceJwt).updateApnsTokens(tokens)
                SharedConfig.defaults.set(fingerprint, forKey: sentKey)
                // The relay stores the tokens either way; it can only use them with APNs set up.
                AppModel.shared.pushState = pairing.canPush ? .registered : .notConfigured
            } catch let e as RelayError where e.isUnauthorized {
                AppModel.shared.onUnauthorized()
            } catch {
                log.warning("token upload failed: \(error.localizedDescription, privacy: .public)")
                AppModel.shared.pushState = .error(error.localizedDescription)
            }
        }
    }

    func forgetUploadedTokens() {
        SharedConfig.defaults.removeObject(forKey: sentKey)
    }
}

extension PushRegistrar: PKPushRegistryDelegate {
    nonisolated func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        let token = pushCredentials.token.map { String(format: "%02x", $0) }.joined()
        MainActor.assumeIsolated {
            voipToken = token
            uploadIfNeeded()
        }
    }

    nonisolated func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        MainActor.assumeIsolated { voipToken = nil }
    }

    /// iOS kills the app (and eventually stops delivering VoIP pushes) unless every VoIP
    /// push reports a call to CallKit before `completion` runs, even an invalid one.
    nonisolated func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload, for type: PKPushType, completion: @escaping () -> Void) {
        MainActor.assumeIsolated {
            let event = PushContent.verifiedEvent(payload.dictionaryPayload, pairing: PairingKeychain.load())
            guard let event, case .callInvite(let invite) = event.body else {
                log.error("VoIP push that is not a valid invite")
                CallController.shared.reportInvalidPush(completion: completion)
                return
            }
            CallController.shared.reportIncoming(invite, source: .push, completion: completion)
        }
    }
}

/// Notification taps and actions: reply to a message, join a call in progress.
@MainActor
final class NotificationRouter: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationRouter()

    static let replyAction = "REPLY"
    static let joinAction = "JOIN"

    func registerCategories() {
        let reply = UNTextInputNotificationAction(
            identifier: Self.replyAction, title: L("reply"), options: [.authenticationRequired],
            textInputButtonTitle: L("send"), textInputPlaceholder: L("reply")
        )
        let join = UNNotificationAction(identifier: Self.joinAction, title: L("call_join"), options: [.foreground])
        UNUserNotificationCenter.current().setNotificationCategories([
            UNNotificationCategory(identifier: "MSG_NEW", actions: [reply], intentIdentifiers: [], options: []),
            UNNotificationCategory(identifier: "CALL_JOINABLE", actions: [join], intentIdentifiers: [], options: []),
        ])
    }

    /// In the foreground the app shows messages itself; calls have CallKit.
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        let category = notification.request.content.categoryIdentifier
        let onMessages = await MainActor.run { AppModel.shared.isShowingMessages }
        if category == "MSG_NEW" && onMessages { return [] }
        return [.banner, .list, .sound]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        let info = response.notification.request.content.userInfo
        let category = response.notification.request.content.categoryIdentifier
        let typed = (response as? UNTextInputNotificationResponse)?.userText
        // iOS keeps the app running until this returns, so a reply is sent before it suspends.
        await handle(action: response.actionIdentifier, category: category, userInfo: info, typed: typed)
    }

    private func handle(action: String, category: String, userInfo info: [AnyHashable: Any], typed: String?) async {
        let event = PushContent.verifiedEvent(info, pairing: PairingKeychain.load())
        var msgId = info["msg_id"] as? String
        if msgId == nil, case .msgNew(let m)? = event?.body { msgId = m.msgId }
        let callId = info["call_id"] as? String ?? event?.callId
        switch action {
        case Self.replyAction:
            if let typed, !typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                await AppModel.shared.replyFromNotification(typed, to: msgId)
            }
        case Self.joinAction:
            if let callId { CallController.shared.join(callId: callId) }
        case UNNotificationDefaultActionIdentifier:
            if category == "CALL_JOINABLE", let callId {
                CallController.shared.join(callId: callId)
            } else if msgId != nil {
                AppModel.shared.selectedTab = .messages
            }
        default:
            break
        }
    }
}
