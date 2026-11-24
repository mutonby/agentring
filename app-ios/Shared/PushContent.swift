import AgentringCore
import Foundation

/// What a verified push says, in words. Used by the notification extension (to fill in
/// the alert) and by the app (local notifications for events that came over the socket).
public enum PushContent {
    public struct Text: Equatable, Sendable {
        public var title: String
        public var body: String
    }

    /// Verifies an APNs payload against the pairing; nil if it isn't from our relay.
    public static func verifiedEvent(_ userInfo: [AnyHashable: Any], pairing: Pairing?) -> Event? {
        guard let pairing else { return nil }
        if case .ok(let event) = PushVerifier.verify(userInfo, expectedRelayId: pairing.relayId, pushSecret: pairing.pushSecret) {
            return event
        }
        return nil
    }

    /// The alert text for an event, or nil to keep the push's own (localized, generic) text.
    public static func text(for event: Event, agentName: String) -> Text? {
        switch event.body {
        case .msgNew(let m):
            return Text(title: agentName, body: m.text)
        case .callMissed:
            return Text(title: agentName, body: String(format: L10n.callMissedFrom, agentName))
        case .callAccept:
            return Text(title: agentName, body: String(format: L10n.callInProgressWith, agentName))
        default:
            return nil
        }
    }
}

/// Strings both targets need. The rest live in the app's Localizable.strings.
public enum L10n {
    static func s(_ key: String) -> String {
        NSLocalizedString(key, tableName: "Shared", bundle: .main, comment: "")
    }
    public static var callMissedFrom: String { s("call_missed_from") }
    public static var callInProgressWith: String { s("call_in_progress_with") }
}

/// Messages the notification extension verified while the app wasn't running, handed
/// to the app through the App Group so the thread is up to date before it syncs.
public enum SharedInbox {
    private static let key = "inbox.messages"

    public static func append(_ m: Message) {
        let d = SharedConfig.defaults
        var list = (d.data(forKey: key)).flatMap { try? JSONDecoder().decode([Message].self, from: $0) } ?? []
        guard !list.contains(where: { $0.msgId == m.msgId }) else { return }
        list.append(m)
        if list.count > 100 { list.removeFirst(list.count - 100) }
        d.set(try? JSONEncoder().encode(list), forKey: key)
    }

    public static func drain() -> [Message] {
        let d = SharedConfig.defaults
        let list = (d.data(forKey: key)).flatMap { try? JSONDecoder().decode([Message].self, from: $0) } ?? []
        d.removeObject(forKey: key)
        return list
    }
}
