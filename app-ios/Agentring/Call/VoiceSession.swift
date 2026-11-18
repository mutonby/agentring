import AVFoundation
import Foundation
import LiveKit
import os

private let log = Logger(subsystem: "dev.agentring.app", category: "voice")

/// One LiveKit room for one call (never reused). CallKit owns the audio session:
/// LiveKit's automatic AVAudioSession handling is off, and its audio engine may only
/// run between CallKit's `didActivate` and `didDeactivate` (see `AudioGate`).
@MainActor
final class VoiceSession: NSObject {
    enum Signal {
        case reconnecting
        case reconnected
        /// The agent left or the connection is gone for good.
        case gone(String)
    }

    /// The relay's LiveKit identity for the agent (`identityOf` in relay/src/routes/calls.ts).
    static let agentIdentity = "agent"

    private let room = Room()
    private var closing = false
    var onSignal: ((Signal) -> Void)?
    /// The agent is in the room (for calls to the agent: it picked up).
    var onAgentInRoom: (() -> Void)?
    private(set) var agentInRoom = false

    override init() {
        super.init()
        room.add(delegate: self)
    }

    /// Warms up DNS/TLS to the LiveKit server while the phone rings.
    func prepare(url: String) {
        Task { try? await room.prepareConnection(url: url) }
    }

    /// Joins and publishes the microphone. Voice settings match the Android app:
    /// 32 kbps Opus with DTX and RED, echo cancellation and noise suppression.
    func connect(url: String, token: String, withMic: Bool) async throws {
        let capture = AudioCaptureOptions(echoCancellation: true, autoGainControl: true, noiseSuppression: true, highpassFilter: true)
        let publish = AudioPublishOptions(encoding: AudioEncoding(maxBitrate: 32_000), dtx: true, red: true)
        let options = RoomOptions(defaultAudioCaptureOptions: capture, defaultAudioPublishOptions: publish, adaptiveStream: false, dynacast: false)
        try await room.connect(url: url, token: token, roomOptions: options)
        if withMic { try await room.localParticipant.setMicrophone(enabled: true) }
        // The agent may have joined before us: no participantDidConnect then.
        if room.remoteParticipants.values.contains(where: { $0.identity?.stringValue == Self.agentIdentity }) {
            markAgentInRoom()
        }
    }

    func setMuted(_ muted: Bool) {
        Task { try? await room.localParticipant.setMicrophone(enabled: !muted) }
    }

    func close() {
        closing = true
        Task { await room.disconnect() }
    }

    private func markAgentInRoom() {
        guard !agentInRoom else { return }
        agentInRoom = true
        onAgentInRoom?()
    }
}

extension VoiceSession: RoomDelegate {
    nonisolated func room(_ room: Room, didUpdateConnectionState connectionState: ConnectionState, from oldConnectionState: ConnectionState) {
        Task { @MainActor in
            switch connectionState {
            case .reconnecting: onSignal?(.reconnecting)
            case .connected where oldConnectionState == .reconnecting: onSignal?(.reconnected)
            default: break
            }
        }
    }

    nonisolated func room(_ room: Room, didDisconnectWithError error: LiveKitError?) {
        Task { @MainActor in
            if !closing { onSignal?(.gone("disconnected: \(error?.localizedDescription ?? "none")")) }
        }
    }

    nonisolated func room(_ room: Room, participantDidConnect participant: RemoteParticipant) {
        let identity = participant.identity?.stringValue
        Task { @MainActor in
            if identity == Self.agentIdentity { markAgentInRoom() }
        }
    }

    nonisolated func room(_ room: Room, participantDidDisconnect participant: RemoteParticipant) {
        let identity = participant.identity?.stringValue
        Task { @MainActor in
            log.info("\(identity ?? "?", privacy: .public) left")
            if identity == Self.agentIdentity && !closing { onSignal?(.gone("agent_left")) }
        }
    }
}

/// CallKit activates the audio session; LiveKit's engine follows it.
enum AudioGate {
    /// Once at launch, before any Room exists.
    static func configure() {
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
        try? AudioManager.shared.setEngineAvailability(.none)
    }

    /// Before CallKit activates the session (answer / start call).
    static func prepareSession() {
        let s = AVAudioSession.sharedInstance()
        do {
            try s.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetooth, .allowBluetoothA2DP])
        } catch {
            log.error("audio session category: \(error.localizedDescription, privacy: .public)")
        }
    }

    static func activated() {
        do {
            try AudioManager.shared.setEngineAvailability(.default)
        } catch {
            log.error("audio engine on: \(error.localizedDescription, privacy: .public)")
        }
    }

    static func deactivated() {
        try? AudioManager.shared.setEngineAvailability(.none)
    }

    static var speakerOn: Bool {
        AVAudioSession.sharedInstance().currentRoute.outputs.contains { $0.portType == .builtInSpeaker }
    }

    @discardableResult
    static func setSpeaker(_ on: Bool) -> Bool {
        do {
            try AVAudioSession.sharedInstance().overrideOutputAudioPort(on ? .speaker : .none)
        } catch {
            log.error("speaker \(on): \(error.localizedDescription, privacy: .public)")
        }
        return speakerOn
    }
}
