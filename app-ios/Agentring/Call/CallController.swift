import AgentringCore
import AVFoundation
@preconcurrency import CallKit
import CryptoKit
import Foundation
import Observation
import os
import UIKit

private let log = Logger(subsystem: "dev.agentring.app", category: "call")

/// What the call screen shows.
struct CallInfo: Equatable, Sendable {
    let callId: String
    let agentName: String
    let reason: String
    let urgent: Bool
    let expiresAt: Int64
    /// CallKit's id for the call: derived from the call id, so the same call reported
    /// twice (VoIP push and WebSocket) is one call.
    var uuid: UUID { CallController.uuid(for: callId) }

    static func of(_ c: Call) -> CallInfo {
        CallInfo(callId: c.callId, agentName: c.agentName, reason: c.reason ?? "", urgent: c.priority == "urgent", expiresAt: 0)
    }
}

enum CallPhase: Equatable {
    case incoming
    /// This phone called the agent: in the room, waiting for it to pick up.
    case outgoing
    case connecting
    case active(since: Date)
    case ended(String)

    var isLive: Bool {
        if case .ended = self { return false }
        return true
    }
}

/// The call state machine, mirroring the Android app's CallManager, on top of CallKit.
/// User actions (answer, hang up, mute) go through CXCallController so the system call
/// UI, the lock screen, headsets and CarPlay stay in sync; CallKit then calls back into
/// the provider delegate, which does the real work.
@MainActor
@Observable
final class CallController: NSObject {
    static let shared = CallController()

    enum InviteSource { case push, socket }

    private(set) var info: CallInfo?
    private(set) var phase: CallPhase?
    private(set) var muted = false
    private(set) var speaker = false
    private(set) var reconnecting = false
    /// A group call from the agent in progress that this phone is not on.
    private(set) var joinOffer: CallInfo?

    @ObservationIgnored private let provider: CXProvider
    @ObservationIgnored private let callController = CXCallController()
    @ObservationIgnored private var seen: [String] = []
    @ObservationIgnored private var session: VoiceSession?
    @ObservationIgnored private var inRoom = false
    /// This phone placed the call (to the agent, or joining one): CallKit needs to be told when it connects.
    @ObservationIgnored private var outgoingToCallKit = false
    @ObservationIgnored private var tasks: [Task<Void, Never>] = []
    @ObservationIgnored private let lastLiveKitURLKey = "call.last_livekit_url"

    private static let clockSkewMs: Int64 = 5_000
    private static let outgoingPollSeconds: UInt64 = 2
    /// Well past the relay's ring timeout (30 s by default).
    private static let outgoingGiveUpSeconds: Double = 75
    private static let endedLingerSeconds: UInt64 = 2

    override init() {
        let config = CXProviderConfiguration()
        config.supportsVideo = false
        config.maximumCallGroups = 1
        config.maximumCallsPerCallGroup = 1
        config.supportedHandleTypes = [.generic]
        config.includesCallsInRecents = false
        if let icon = UIImage(named: "CallKitIcon")?.pngData() { config.iconTemplateImageData = icon }
        provider = CXProvider(configuration: config)
        super.init()
        provider.setDelegate(self, queue: nil)
    }

    nonisolated static func uuid(for callId: String) -> UUID {
        var bytes = Array(SHA256.hash(data: Data(callId.utf8)).prefix(16))
        bytes[6] = (bytes[6] & 0x0F) | 0x50 // name-based UUID shape (version 5, variant RFC 4122)
        bytes[8] = (bytes[8] & 0x3F) | 0x80
        return UUID(uuid: (bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
                           bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]))
    }

    private var api: RelayAPI? { AppModel.shared.api }
    private func isCurrent(_ callId: String) -> Bool { info?.callId == callId }
    private var onACall: Bool { phase?.isLive == true }

    // MARK: - incoming

    /// A VoIP push that isn't a valid invite still has to show a call (iOS rule): show one and end it at once.
    func reportInvalidPush(completion: @escaping () -> Void) {
        let uuid = UUID()
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: "agentring")
        provider.reportNewIncomingCall(with: uuid, update: update) { [provider] _ in
            provider.reportCall(with: uuid, endedAt: nil, reason: .failed)
            completion()
        }
    }

    /// Rings for an invite. From a VoIP push the call must be reported to CallKit right
    /// away, even when it can't ring (then it ends at once); from the socket, a replayed
    /// invite is checked with the relay first.
    func reportIncoming(_ invite: CallInvite, source: InviteSource, completion: (() -> Void)? = nil) {
        let now = Int64(Date().timeIntervalSince1970 * 1000)
        let call = CallInfo(callId: invite.callId, agentName: invite.agentName, reason: invite.reason, urgent: invite.isUrgent, expiresAt: invite.expiresAt)
        let alreadySeen = seen.contains(invite.callId)
        let canRing = invite.direction == "agent_to_user" && now <= invite.expiresAt + Self.clockSkewMs && !alreadySeen && !onACall

        if source == .socket {
            guard canRing else { return }
            remember(invite.callId)
            Task {
                // A WebSocket replay may carry an invite that was answered or canceled since.
                let state = try? await api?.getCall(invite.callId)
                guard state?.isRinging == true, !onACall else { return }
                ring(call, completion: nil)
            }
            return
        }

        guard canRing else {
            if alreadySeen && isCurrent(invite.callId) {
                // Already ringing (the socket was faster): CallKit already shows it.
                completion?()
            } else {
                log.info("VoIP invite \(invite.callId, privacy: .public) can't ring; ending it")
                reportAndEnd(call, completion: completion)
            }
            return
        }
        remember(invite.callId)
        ring(call, completion: completion)
    }

    private func remember(_ callId: String) {
        seen.append(callId)
        if seen.count > 50 { seen.removeFirst(seen.count - 50) }
        // One call at a time: a new invite means any older offer is over.
        if joinOffer?.callId != callId { joinOffer = nil }
    }

    private func update(for call: CallInfo) -> CXCallUpdate {
        let u = CXCallUpdate()
        u.remoteHandle = CXHandle(type: .generic, value: call.agentName)
        u.localizedCallerName = call.urgent ? "\(call.agentName) · \(L("call_urgent"))" : call.agentName
        u.hasVideo = false
        u.supportsHolding = false
        u.supportsGrouping = false
        u.supportsUngrouping = false
        u.supportsDTMF = false
        return u
    }

    private func reportAndEnd(_ call: CallInfo, completion: (() -> Void)?) {
        let uuid = UUID()
        provider.reportNewIncomingCall(with: uuid, update: update(for: call)) { [provider] _ in
            provider.reportCall(with: uuid, endedAt: nil, reason: .failed)
            completion?()
        }
    }

    private func ring(_ call: CallInfo, completion: (() -> Void)?) {
        info = call
        phase = .incoming
        muted = false
        speaker = false
        reconnecting = false
        outgoingToCallKit = false
        provider.reportNewIncomingCall(with: call.uuid, update: update(for: call)) { error in
            Task { @MainActor in
                completion?()
                if let error {
                    // Do Not Disturb / blocked: other phones may still answer, so don't decline on the relay.
                    log.warning("CallKit refused the call: \(error.localizedDescription, privacy: .public)")
                    self.finish(call, message: L("call_missed"), reportReason: nil)
                    return
                }
                self.startedRinging(call)
            }
        }
    }

    private func startedRinging(_ call: CallInfo) {
        log.info("ringing \(call.callId, privacy: .public)")
        tasks.append(Task {
            for _ in 0..<2 {
                do {
                    try await api?.reportRinging(call.callId)
                    return
                } catch {
                    log.warning("report ringing failed: \(error.localizedDescription, privacy: .public)")
                }
            }
        })
        // Don't ring past the relay's deadline even if the "missed" event never arrives.
        tasks.append(Task {
            let ms = call.expiresAt - Int64(Date().timeIntervalSince1970 * 1000) + 2_000
            try? await Task.sleep(nanoseconds: UInt64(max(0, ms)) * 1_000_000)
            guard !Task.isCancelled, isCurrent(call.callId), phase == .incoming else { return }
            finish(call, message: L("call_missed"), reportReason: .unanswered)
            // Another phone may have answered: then offer to join instead.
            if let c = try? await api?.getCall(call.callId) { offerJoin(c) }
        })
        // Build the WebRTC stack and warm the connection while the user decides.
        let s = VoiceSession()
        session = s
        if let url = SharedConfig.defaults.string(forKey: lastLiveKitURLKey) { s.prepare(url: url) }
    }

    /// Accept while ringing; if the ring window closed meanwhile, join the call instead.
    private func acceptOrJoin(_ callId: String) async throws -> JoinInfo {
        guard let api else { throw ProtocolError("not paired") }
        do {
            return try await api.acceptCall(callId).join
        } catch let e as RelayError where e.code == "invalid_state" {
            return try await api.joinCall(callId).join
        }
    }

    private func answered(_ call: CallInfo) {
        phase = .connecting
        tasks.append(Task {
            do {
                let join = try await acceptOrJoin(call.callId)
                await connect(call, join)
            } catch let e as RelayError {
                log.warning("accept failed: \(e.code, privacy: .public)")
                let gone = e.code == "invalid_state" || e.code == "call_ended"
                finish(call, message: L(gone ? "call_unavailable" : "call_failed"), reportReason: .failed)
            } catch {
                finish(call, message: L("call_failed"), reportReason: .failed)
            }
        })
    }

    // MARK: - joining a call in progress

    /// "Join the call" from the notification or the Home banner.
    func join(callId: String) {
        if phase == .incoming, isCurrent(callId) {
            requestAnswer()
            return
        }
        guard !onACall else { return }
        remember(callId) // a replayed invite must not ring now
        joinOffer = nil
        let placeholder = CallInfo(callId: callId, agentName: AppModel.shared.agentName, reason: "", urgent: false, expiresAt: 0)
        info = placeholder
        phase = .connecting
        muted = false
        speaker = false
        outgoingToCallKit = true
        startOutgoingInCallKit(placeholder)
        tasks.append(Task {
            do {
                guard let api else { throw ProtocolError("not paired") }
                let joined = try await api.joinCall(callId)
                guard isCurrent(callId), phase == .connecting else {
                    // Hung up before the relay answered: leave again so the relay doesn't count us in.
                    if let c = try? await api.endCall(callId) { offerJoin(c) }
                    return
                }
                info = CallInfo.of(joined.call)
                await connect(info!, joined.join)
            } catch {
                let over = (error as? RelayError)?.code == "call_ended"
                finish(placeholder, message: L(over ? "call_unavailable" : "call_failed"), reportReason: .failed)
                if !over { recheckJoinable(callId) }
            }
        })
    }

    // MARK: - calling the agent

    /// Calls the agent. Returns nil once it is ringing, or a message to show.
    func callAgent() async -> String? {
        switch phase {
        case .outgoing, .active: return nil // already talking to it: just show the call
        case .incoming, .connecting: return L("call_agent_busy")
        case .ended, nil: break
        }
        guard let api else { return L("call_agent_needs_relay") }
        let created: CallWithJoin
        do {
            created = try await api.createCall()
        } catch let e as RelayError {
            log.warning("create call failed: \(e.status) \(e.code, privacy: .public)")
            switch e.code {
            case "agent_offline": return L("call_agent_offline")
            case "busy": return L("call_agent_busy")
            case "quota_exceeded": return L("call_agent_quota")
            case "calls_unavailable": return L("call_agent_unavailable")
            default: return L("call_failed")
            }
        } catch {
            return L("call_failed")
        }
        let call = CallInfo.of(created.call)
        remember(call.callId)
        if onACall {
            // An agent call came in meanwhile: give up ours.
            _ = try? await api.endCall(call.callId)
            return L("call_agent_busy")
        }
        joinOffer = nil
        inRoom = false
        info = call
        phase = .outgoing
        muted = false
        speaker = false
        outgoingToCallKit = true
        startOutgoingInCallKit(call)
        tasks.append(Task { await connect(call, created.join) })
        tasks.append(Task { await pollOutgoing(call) })
        return nil
    }

    private func startOutgoingInCallKit(_ call: CallInfo) {
        let action = CXStartCallAction(call: call.uuid, handle: CXHandle(type: .generic, value: call.agentName))
        action.isVideo = false
        callController.request(CXTransaction(action: action)) { error in
            if let error { log.error("start call: \(error.localizedDescription, privacy: .public)") }
        }
        provider.reportCall(with: call.uuid, updated: update(for: call))
    }

    /// Backup for the WebSocket while calling: the socket may be down.
    private func pollOutgoing(_ call: CallInfo) async {
        let deadline = Date().addingTimeInterval(Self.outgoingGiveUpSeconds)
        while isCurrent(call.callId), phase == .outgoing {
            try? await Task.sleep(nanoseconds: Self.outgoingPollSeconds * 1_000_000_000)
            if Task.isCancelled { return }
            if Date() > deadline {
                _ = try? await api?.endCall(call.callId)
                finish(call, message: L("call_agent_no_answer"), reportReason: .unanswered)
                return
            }
            if let c = try? await api?.getCall(call.callId) { onOutgoingState(call.callId, c.state) }
        }
    }

    private func onOutgoingState(_ callId: String, _ state: String) {
        guard let info, info.callId == callId, phase?.isLive == true else { return }
        switch state {
        case "active": onAgentAnswered(callId)
        case "declined": finish(info, message: L("call_agent_declined"), reportReason: .remoteEnded)
        case "missed": finish(info, message: L("call_agent_no_answer"), reportReason: .unanswered)
        case "canceled", "ended": finish(info, message: L("call_ended"), reportReason: .remoteEnded)
        default: break
        }
    }

    private func onAgentAnswered(_ callId: String) {
        guard phase == .outgoing, let info, info.callId == callId else { return }
        log.info("the agent answered \(callId, privacy: .public)")
        // Not in the room yet: connect() finishes the switch to active.
        if inRoom { goActive(info) } else { phase = .connecting }
    }

    // MARK: - media

    /// Joins the LiveKit room with a fresh token, for an answered, joined or placed call.
    private func connect(_ call: CallInfo, _ join: JoinInfo) async {
        do {
            try RelayURLPolicy.checkLiveKit(join.livekitUrl)
        } catch {
            log.error("refusing LiveKit URL from relay")
            _ = try? await api?.endCall(call.callId)
            finish(call, message: L("call_failed"), reportReason: .failed)
            return
        }
        SharedConfig.defaults.set(join.livekitUrl, forKey: lastLiveKitURLKey)
        let s = session ?? VoiceSession()
        session = s
        s.onSignal = { [weak self] sig in self?.onVoiceSignal(sig, call) }
        s.onAgentInRoom = { [weak self] in
            guard let self, self.inRoom else { return }
            self.onAgentAnswered(call.callId)
        }
        let t0 = Date()
        do {
            let mic = AVAudioApplication.shared.recordPermission == .granted
            try await s.connect(url: join.livekitUrl, token: join.livekitToken, withMic: mic)
        } catch {
            log.warning("LiveKit connect failed: \(error.localizedDescription, privacy: .public)")
            let c = try? await api?.endCall(call.callId)
            finish(call, message: L("call_failed"), reportReason: .failed)
            if let c { offerJoin(c) }
            return
        }
        log.info("in the room after \(Int(Date().timeIntervalSince(t0) * 1000)) ms")
        guard isCurrent(call.callId) else { return }
        inRoom = true
        switch phase {
        case .connecting: goActive(info ?? call)
        // Calling the agent: stay "Calling…" until it picks up, unless it already did.
        case .outgoing: if s.agentInRoom { onAgentAnswered(call.callId) }
        default: break
        }
    }

    private func goActive(_ call: CallInfo) {
        phase = .active(since: Date())
        speaker = AudioGate.speakerOn
        if outgoingToCallKit { provider.reportOutgoingCall(with: call.uuid, connectedAt: nil) }
    }

    private func onVoiceSignal(_ sig: VoiceSession.Signal, _ call: CallInfo) {
        guard isCurrent(call.callId), phase?.isLive == true else { return }
        switch sig {
        case .reconnecting: reconnecting = true
        case .reconnected: reconnecting = false
        case .gone(let why):
            log.info("voice session gone: \(why, privacy: .public)")
            let calling = phase == .outgoing
            Task {
                // Lost the connection while others are still talking: offer to rejoin.
                if let c = try? await api?.endCall(call.callId) { offerJoin(c) }
            }
            finish(call, message: L(calling ? "call_failed" : "call_ended_by_agent"), reportReason: .remoteEnded)
        }
    }

    // MARK: - user actions (through CallKit)

    func requestAnswer() {
        guard phase == .incoming, let info else { return }
        callController.request(CXTransaction(action: CXAnswerCallAction(call: info.uuid))) { error in
            if let error { log.error("answer: \(error.localizedDescription, privacy: .public)") }
        }
    }

    func requestHangUp() {
        guard let info, phase?.isLive == true else { return }
        callController.request(CXTransaction(action: CXEndCallAction(call: info.uuid))) { [weak self] error in
            guard error != nil else { return }
            // CallKit doesn't know the call (e.g. it refused it): hang up directly.
            Task { @MainActor in self?.hangUp() }
        }
    }

    func requestMute(_ on: Bool) {
        guard let info else { return }
        callController.request(CXTransaction(action: CXSetMutedCallAction(call: info.uuid, muted: on))) { _ in }
    }

    func toggleSpeaker() {
        guard case .active = phase else { return }
        speaker = AudioGate.setSpeaker(!speaker)
    }

    private func decline() {
        guard phase == .incoming, let info else { return }
        // If another phone is already on the call, this one can still join it later.
        Task { if let c = try? await api?.declineCall(info.callId) { offerJoin(c) } }
        finish(info, message: L("call_declined"), reportReason: nil)
    }

    private func hangUp() {
        guard let info, let phase, phase.isLive else { return }
        if phase == .incoming {
            decline()
            return
        }
        // While calling the agent this cancels the call (relay: `canceled`). Leaving a
        // group call: the others may still be on it, so this phone can rejoin.
        Task { if let c = try? await api?.endCall(info.callId) { offerJoin(c) } }
        finish(info, message: L("call_ended"), reportReason: nil)
    }

    // MARK: - relay events

    /// Relay events (socket or push) about a call we may be showing or may join.
    func onEvent(_ event: Event) {
        switch event.body {
        case .callEnd(let e) where joinOffer?.callId == e.callId: joinOffer = nil
        case .callMissed(let e) where joinOffer?.callId == e.callId: joinOffer = nil
        case .callAccept(let a) where a.by == "user" && a.deviceId != AppModel.shared.pairing?.deviceId
            && !(isCurrent(a.callId) && onACall) && joinOffer?.callId != a.callId:
            // Another phone answered or joined an agent call: offer to join it.
            recheckJoinable(a.callId)
        default: break
        }
        guard let info, let phase else { return }
        if phase == .outgoing {
            switch event.body {
            case .callAccept(let a) where a.callId == info.callId: onOutgoingState(info.callId, "active")
            case .callDecline(let a) where a.callId == info.callId: onOutgoingState(info.callId, "declined")
            case .callMissed(let m) where m.callId == info.callId: onOutgoingState(info.callId, "missed")
            case .callEnd(let e) where e.callId == info.callId: onOutgoingState(info.callId, "ended")
            default: break
            }
            return
        }
        switch event.body {
        // Group calls: another phone answering or declining doesn't stop ours ringing.
        case .callEnd(let e) where e.callId == info.callId:
            let quota = e.reason == "quota" // the relay cut the call: the month's minutes ran out
            if case .ended = phase {
                if quota { self.phase = .ended(L("call_ended_quota")) }
            } else {
                let missed = phase == .incoming
                finish(info, message: L(missed ? "call_missed" : quota ? "call_ended_quota" : "call_ended_by_agent"),
                       reportReason: missed ? CXCallEndedReason.unanswered : CXCallEndedReason.remoteEnded)
            }
        case .callMissed(let m) where m.callId == info.callId && phase == .incoming:
            finish(info, message: L("call_missed"), reportReason: .unanswered)
        default: break
        }
    }

    /// The relay's view of the open call (`active_call` in /v1/status), e.g. on foreground.
    func onActiveCall(_ call: Call?) {
        if let call { offerJoin(call) } else { joinOffer = nil }
    }

    private func offerJoin(_ call: Call) {
        guard call.isJoinable else {
            if joinOffer?.callId == call.callId { joinOffer = nil }
            return
        }
        if onACall, isCurrent(call.callId) { return }
        joinOffer = CallInfo.of(call)
    }

    private func recheckJoinable(_ callId: String) {
        Task { if let c = try? await api?.getCall(callId) { offerJoin(c) } }
    }

    // MARK: - teardown

    /// `reportReason` tells CallKit why the call ended when the user didn't end it
    /// through CallKit (nil when CallKit already knows: an end/decline action).
    private func finish(_ call: CallInfo, message: String, reportReason: CXCallEndedReason?) {
        guard isCurrent(call.callId), phase?.isLive == true else { return }
        log.info("call \(call.callId, privacy: .public) finished: \(message, privacy: .public)")
        tasks.forEach { $0.cancel() }
        tasks.removeAll()
        session?.close()
        session = nil
        inRoom = false
        reconnecting = false
        if let reportReason { provider.reportCall(with: call.uuid, endedAt: nil, reason: reportReason) }
        if speaker { AudioGate.setSpeaker(false) }
        speaker = false
        muted = false
        outgoingToCallKit = false
        phase = .ended(message)
        Task {
            try? await Task.sleep(nanoseconds: Self.endedLingerSeconds * 1_000_000_000)
            if case .ended = phase, isCurrent(call.callId) {
                phase = nil
                info = nil
            }
        }
    }
}

// MARK: - CXProviderDelegate

extension CallController: CXProviderDelegate {
    nonisolated func providerDidReset(_ provider: CXProvider) {
        MainActor.assumeIsolated {
            if let info, phase?.isLive == true { finish(info, message: L("call_ended"), reportReason: nil) }
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
        MainActor.assumeIsolated {
            guard let info, info.uuid == action.callUUID, phase == .incoming else {
                action.fail()
                return
            }
            AudioGate.prepareSession()
            answered(info)
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        MainActor.assumeIsolated {
            if let info, info.uuid == action.callUUID { hangUp() }
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        MainActor.assumeIsolated {
            AudioGate.prepareSession()
            provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        MainActor.assumeIsolated {
            session?.setMuted(action.isMuted)
            muted = action.isMuted
            action.fulfill()
        }
    }

    nonisolated func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        MainActor.assumeIsolated { AudioGate.activated() }
    }

    nonisolated func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        MainActor.assumeIsolated { AudioGate.deactivated() }
    }
}
