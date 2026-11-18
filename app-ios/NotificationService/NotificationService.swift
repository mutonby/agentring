import AgentringCore
import UserNotifications

/// Runs for every alert push with `mutable-content` (messages, missed calls, "join the
/// call"). The relay puts the real text only inside the signed event; this verifies the
/// signature with the pairing's push secret and swaps in the text. If anything fails,
/// the push keeps its generic, localized text, so nothing unverified is ever shown.
final class NotificationService: UNNotificationServiceExtension {
    private var contentHandler: ((UNNotificationContent) -> Void)?
    private var best: UNMutableNotificationContent?

    override func didReceive(_ request: UNNotificationRequest, withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        self.contentHandler = contentHandler
        guard let content = request.content.mutableCopy() as? UNMutableNotificationContent else {
            contentHandler(request.content)
            return
        }
        best = content
        let pairing = PairingKeychain.load()
        guard let event = PushContent.verifiedEvent(request.content.userInfo, pairing: pairing), let pairing else {
            contentHandler(content)
            return
        }
        if let text = PushContent.text(for: event, agentName: pairing.agentName) {
            content.title = text.title
            content.body = text.body
        }
        if case .msgNew(let m) = event.body {
            content.userInfo["msg_id"] = m.msgId
            SharedInbox.append(m.toMessage(ts: event.ts))
        }
        if let callId = event.callId { content.userInfo["call_id"] = callId }
        contentHandler(content)
    }

    override func serviceExtensionTimeWillExpire() {
        if let contentHandler, let best { contentHandler(best) }
    }
}
