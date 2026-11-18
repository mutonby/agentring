import AgentringCore
import Foundation
import Observation

/// The message thread, kept in a small JSON file (one agent, a few hundred messages).
@MainActor
@Observable
final class MessageStore {
    private(set) var messages: [Message] = []
    /// Messages we read locally but haven't told the relay yet don't need tracking: the
    /// relay is the source of truth and the next sync brings `read_at` back.
    @ObservationIgnored private let maxKept = 500

    private var file: URL {
        let dir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("messages.json")
    }

    func load() {
        guard let data = try? Data(contentsOf: file), let list = try? JSONDecoder().decode([Message].self, from: data) else { return }
        messages = list
    }

    func merge(_ incoming: [Message]) {
        guard !incoming.isEmpty else { return }
        var byId = Dictionary(messages.map { ($0.msgId, $0) }, uniquingKeysWith: { a, _ in a })
        for m in incoming {
            if let old = byId[m.msgId] {
                // Keep the longer text (a push may have been truncated) and any read mark.
                var merged = m.text.count >= old.text.count ? m : old
                merged.readAt = m.readAt ?? old.readAt
                byId[m.msgId] = merged
            } else {
                byId[m.msgId] = m
            }
        }
        messages = Array(byId.values.sorted { ($0.createdAt, $0.msgId) < ($1.createdAt, $1.msgId) }.suffix(maxKept))
        save()
    }

    func markRead(_ msgId: String, at ts: Int64) {
        guard let i = messages.firstIndex(where: { $0.msgId == msgId }), messages[i].readAt == nil else { return }
        messages[i].readAt = ts
        save()
    }

    func unreadFromAgent() -> [Message] {
        messages.filter { $0.isFromAgent && $0.readAt == nil }
    }

    func clear() {
        messages = []
        try? FileManager.default.removeItem(at: file)
    }

    private func save() {
        guard let data = try? JSONEncoder().encode(messages) else { return }
        try? data.write(to: file, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
    }
}
