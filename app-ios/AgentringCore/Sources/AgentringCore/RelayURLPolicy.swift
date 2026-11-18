import Foundation

/// TLS is mandatory, except for plain-HTTP relays on a private network (self-hosting
/// on a LAN or over Tailscale, local development). Same rule as the Android app.
public enum RelayURLPolicy {
    public static func check(_ url: String) throws {
        guard let u = URLComponents(string: url), let host = u.host, !host.isEmpty else {
            throw ProtocolError("Invalid relay URL")
        }
        switch u.scheme {
        case "https": return
        case "http":
            guard isPrivateHost(host) else {
                throw ProtocolError("The relay must use https:// (http is only allowed on a local network)")
            }
        default: throw ProtocolError("The relay URL must start with https://")
        }
    }

    /// The LiveKit URL carries the call's join token: wss:// unless the server is on a private network.
    public static func checkLiveKit(_ url: String) throws {
        guard let u = URLComponents(string: url), let host = u.host, !host.isEmpty else {
            throw ProtocolError("Invalid LiveKit URL")
        }
        switch u.scheme {
        case "wss", "https": return
        case "ws", "http":
            guard isPrivateHost(host) else { throw ProtocolError("LiveKit must use wss:// (ws is only allowed on a local network)") }
        default: throw ProtocolError("Invalid LiveKit URL scheme")
        }
    }

    public static func isPrivateHost(_ host: String) -> Bool {
        let h = host.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if h == "localhost" || h.hasSuffix(".local") || h == "::1" { return true }
        let parts = h.split(separator: ".", omittingEmptySubsequences: false).map { Int($0) }
        guard parts.count == 4, parts.allSatisfy({ $0 != nil && (0...255).contains($0!) }) else { return false }
        let a = parts[0]!, b = parts[1]!
        return a == 10 || a == 127 || (a == 192 && b == 168) || (a == 172 && (16...31).contains(b)) || (a == 100 && (64...127).contains(b))
    }
}
