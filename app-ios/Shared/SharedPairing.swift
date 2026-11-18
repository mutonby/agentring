import Foundation
import Security

/// The pairing, shared between the app and its notification extension through a
/// keychain access group (the device JWT and push secret never touch UserDefaults).
public struct Pairing: Codable, Equatable, Sendable {
    public var relayUrl: String
    public var deviceId: String
    public var deviceJwt: String
    public var pushSecret: String
    public var relayId: String
    public var agentName: String
    /// The relay can wake this iPhone through APNs.
    public var canPush: Bool
    /// agentring cloud (agentring.io) rather than the user's own relay.
    public var isCloud: Bool { relayUrl.contains("agentring.io/t/") }

    public init(relayUrl: String, deviceId: String, deviceJwt: String, pushSecret: String, relayId: String, agentName: String, canPush: Bool) {
        self.relayUrl = relayUrl
        self.deviceId = deviceId
        self.deviceJwt = deviceJwt
        self.pushSecret = pushSecret
        self.relayId = relayId
        self.agentName = agentName
        self.canPush = canPush
    }
}

public enum SharedConfig {
    /// Must match the App Group in both targets' entitlements.
    public static let appGroup = "group.dev.agentring.app"
    /// Keychain access group without the team prefix (Xcode adds `$(AppIdentifierPrefix)`).
    public static let keychainGroupSuffix = "dev.agentring.shared"
    public static let cloudURL = "https://agentring.io"

    public static var defaults: UserDefaults { UserDefaults(suiteName: appGroup) ?? .standard }

    /// The full access group, read from the app's own entitlements at runtime
    /// (`AppIdentifierPrefix` is the team id plus a dot).
    static var keychainGroup: String? {
        guard let prefix = Bundle.main.object(forInfoDictionaryKey: "AppIdentifierPrefix") as? String, !prefix.isEmpty else { return nil }
        return prefix + keychainGroupSuffix
    }
}

public enum PairingKeychain {
    private static let service = "dev.agentring.pairing"
    private static let account = "current"

    private static func query() -> [String: Any] {
        var q: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        if let group = SharedConfig.keychainGroup { q[kSecAttrAccessGroup as String] = group }
        return q
    }

    public static func load() -> Pairing? {
        var q = query()
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var out: CFTypeRef?
        guard SecItemCopyMatching(q as CFDictionary, &out) == errSecSuccess, let data = out as? Data else { return nil }
        return try? JSONDecoder().decode(Pairing.self, from: data)
    }

    @discardableResult
    public static func save(_ p: Pairing) -> Bool {
        guard let data = try? JSONEncoder().encode(p) else { return false }
        SecItemDelete(query() as CFDictionary)
        var q = query()
        q[kSecValueData as String] = data
        // The notification extension runs while the phone is locked: readable after first unlock.
        q[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        return SecItemAdd(q as CFDictionary, nil) == errSecSuccess
    }

    public static func clear() {
        SecItemDelete(query() as CFDictionary)
    }
}
