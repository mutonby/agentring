import CryptoKit
import Foundation

/// Verifies push payloads: only events signed by the paired relay are accepted.
/// sig = base64url(HMAC-SHA256(base64url_decode(push_secret), utf8(event))).
public enum PushVerifier {
    public enum Result: Equatable, Sendable {
        case ok(Event)
        case rejected(String)
    }

    public static func sign(pushSecret: String, eventJSON: String) -> String? {
        guard let key = Base64URL.decode(pushSecret) else { return nil }
        let mac = HMAC<SHA256>.authenticationCode(for: Data(eventJSON.utf8), using: SymmetricKey(data: key))
        return Base64URL.encode(Data(mac))
    }

    /// `data` is the APNs payload (or any string map with v, relay_id, event, sig).
    public static func verify(_ data: [AnyHashable: Any], expectedRelayId: String, pushSecret: String) -> Result {
        guard data["v"] as? String == "1" else { return .rejected("unsupported payload version") }
        guard data["relay_id"] as? String == expectedRelayId else { return .rejected("payload from a different relay") }
        guard let eventJSON = data["event"] as? String else { return .rejected("missing event") }
        guard let sig = data["sig"] as? String else { return .rejected("missing signature") }
        guard let key = Base64URL.decode(pushSecret), let given = Base64URL.decode(sig) else { return .rejected("bad signature") }
        // CryptoKit compares in constant time.
        guard HMAC<SHA256>.isValidAuthenticationCode(given, authenticating: Data(eventJSON.utf8), using: SymmetricKey(data: key)) else {
            return .rejected("bad signature")
        }
        do {
            return .ok(try Event.parse(eventJSON))
        } catch {
            return .rejected("malformed event")
        }
    }
}

public enum Base64URL {
    public static func encode(_ data: Data) -> String {
        data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    public static func decode(_ s: String) -> Data? {
        var b = s.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        while b.count % 4 != 0 { b.append("=") }
        return Data(base64Encoded: b)
    }
}
