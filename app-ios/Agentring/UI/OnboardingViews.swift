import AVFoundation
import SwiftUI
import UIKit
import UserNotifications

/// Welcome → how to connect → pair → make sure it rings → try it.
struct OnboardingFlow: View {
    @Environment(AppModel.self) private var model
    enum Step: Hashable { case welcome, how, pair(cloud: Bool), calls, done }
    @State private var path: [Step] = []
    @State private var cloud = true

    var body: some View {
        NavigationStack(path: $path) {
            WelcomeView { path.append(.how) }
                .navigationDestination(for: Step.self) { step in
                    switch step {
                    case .welcome, .how:
                        HowConnectView { cloud in
                            self.cloud = cloud
                            path.append(.pair(cloud: cloud))
                        }
                    case .pair(let cloud):
                        PairView(cloud: cloud) { path.append(.calls) }
                    case .calls:
                        CallsReadyView { path.append(.done) }
                    case .done:
                        DoneView(cloud: cloud) { model.finishOnboarding() }
                    }
                }
        }
    }
}

private struct StepHeader: View {
    let step: Int
    let title: String
    let body_: String
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(String(format: L("step_of"), step, 4)).font(.footnote.weight(.semibold)).foregroundStyle(Brand.primary)
            Text(title).font(.largeTitle.bold())
            Text(body_).foregroundStyle(Brand.secondaryText)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

struct WelcomeView: View {
    let onNext: () -> Void
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                Image("Logo").resizable().scaledToFit().frame(width: 72, height: 72).accessibilityHidden(true)
                Text(L("welcome_title")).font(.largeTitle.bold())
                Text(L("welcome_body")).foregroundStyle(Brand.secondaryText)
                VStack(spacing: 12) {
                    feature("message.fill", L("feature_messages_title"), L("feature_messages_body"))
                    feature("phone.fill", L("feature_calls_title"), L("feature_calls_body"))
                    feature("waveform", L("feature_callback_title"), L("feature_callback_body"))
                }
            }
            .padding(24)
        }
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 8) {
                Button(L("get_started"), action: onNext).buttonStyle(PrimaryButtonStyle())
                Text(L("welcome_footer")).font(.footnote).foregroundStyle(Brand.secondaryText)
            }
            .padding(24)
            .background(Brand.background)
        }
        .background(Brand.background)
    }

    private func feature(_ icon: String, _ title: String, _ body: String) -> some View {
        Card {
            HStack(alignment: .top, spacing: 14) {
                Image(systemName: icon).font(.title3).foregroundStyle(Brand.primary).frame(width: 28)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.headline)
                    Text(body).font(.subheadline).foregroundStyle(Brand.secondaryText)
                }
            }
        }
    }
}

struct HowConnectView: View {
    let onPick: (Bool) -> Void
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                StepHeader(step: 1, title: L("how_title"), body_: L("how_body"))
                option(icon: "cloud.fill", title: L("how_cloud_title"), badge: L("how_cloud_badge"), body: L("how_cloud_body")) { onPick(true) }
                option(icon: "server.rack", title: L("how_self_title"), badge: L("how_self_badge"), body: L("how_self_body")) { onPick(false) }
                Text(L("how_hint")).font(.footnote).foregroundStyle(Brand.secondaryText)
            }
            .padding(24)
        }
        .background(Brand.background)
    }

    private func option(icon: String, title: String, badge: String, body: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Card {
                HStack(spacing: 10) {
                    Image(systemName: icon).foregroundStyle(Brand.primary)
                    Text(title).font(.headline).foregroundStyle(.primary)
                    Text(badge).font(.caption2.bold()).padding(.horizontal, 8).padding(.vertical, 3)
                        .background(Brand.primaryContainer, in: Capsule()).foregroundStyle(Brand.onPrimaryContainer)
                    Spacer()
                    Image(systemName: "chevron.right").foregroundStyle(Brand.secondaryText)
                }
                Text(body).font(.subheadline).foregroundStyle(Brand.secondaryText).multilineTextAlignment(.leading)
            }
        }
        .buttonStyle(.plain)
    }
}

struct PairView: View {
    @Environment(AppModel.self) private var model
    let cloud: Bool
    let onPaired: () -> Void
    @State private var scanning = false
    @State private var pasting = false
    @State private var pasted = ""
    @State private var busy = false
    @State private var error: String?
    @State private var pairedWith: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                StepHeader(step: 2, title: L("pair_title"), body_: L(cloud ? "pair_body_cloud" : "pair_body_self"))
                Card {
                    if cloud {
                        StepRow(number: 1, title: L("pair_cloud_1")) {
                            Text(L("pair_cloud_1_body")).font(.subheadline).foregroundStyle(Brand.secondaryText)
                            Text("agentring.io").font(.subheadline.bold()).textSelection(.enabled)
                        }
                        StepRow(number: 2, title: L("pair_cloud_2")) {
                            Text(L("pair_cloud_2_body")).font(.subheadline).foregroundStyle(Brand.secondaryText)
                        }
                    } else {
                        StepRow(number: 1, title: L("pair_self_1")) {
                            Text(L("pair_self_1_body")).font(.subheadline).foregroundStyle(Brand.secondaryText)
                            CommandBox(command: "docker compose up -d")
                        }
                        StepRow(number: 2, title: L("pair_self_2")) {
                            Text(L("pair_self_2_body")).font(.subheadline).foregroundStyle(Brand.secondaryText)
                            CommandBox(command: "agentring pair")
                        }
                        Text(L("pair_self_ios_push")).font(.footnote).foregroundStyle(Brand.secondaryText)
                    }
                    StepRow(number: 3, title: L("pair_scan_step")) {
                        Text(L("pair_scan_step_body")).font(.subheadline).foregroundStyle(Brand.secondaryText)
                    }
                }
                if let error {
                    Label(error, systemImage: "exclamationmark.triangle.fill").foregroundStyle(Brand.danger).font(.subheadline)
                }
            }
            .padding(24)
        }
        .background(Brand.background)
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 10) {
                Button {
                    error = nil
                    scanning = true
                } label: {
                    if busy { ProgressView().tint(.white) } else { Label(L("scan_qr"), systemImage: "qrcode.viewfinder") }
                }
                .buttonStyle(PrimaryButtonStyle())
                .disabled(busy)
                Button(L("or_paste")) { pasting = true }.disabled(busy)
            }
            .padding(24)
            .background(Brand.background)
        }
        .sheet(isPresented: $scanning) {
            QRScannerSheet { code in
                scanning = false
                pair(code)
            }
        }
        .alert(L("pairing_code"), isPresented: $pasting) {
            TextField(L("pairing_code"), text: $pasted)
            Button(L("pair")) { pair(pasted) }
            Button(L("cancel"), role: .cancel) {}
        }
        .alert(pairedWith.map { String(format: L("done_title"), $0) } ?? "", isPresented: Binding(get: { pairedWith != nil }, set: { if !$0 { pairedWith = nil } })) {
            Button(L("continue_")) {
                pairedWith = nil
                onPaired()
            }
        } message: {
            if model.pairing?.canPush == false { Text(L("done_no_push")) }
        }
    }

    private func pair(_ code: String) {
        busy = true
        Task {
            defer { busy = false }
            do {
                try await model.pair(code: code)
                pairedWith = model.agentName
            } catch {
                self.error = error.localizedDescription
            }
        }
    }
}

/// Notifications and microphone: what a call needs on iOS (CallKit handles the lock screen).
struct CallsReadyView: View {
    let onNext: () -> Void
    @State private var notifications: UNAuthorizationStatus = .notDetermined
    @State private var mic: AVAudioApplication.recordPermission = .undetermined
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                StepHeader(step: 3, title: L("calls_title"), body_: L("calls_body_ios"))
                Card {
                    PermissionRow(title: L("perm_notifications"), granted: notifications == .authorized || notifications == .provisional) {
                        if notifications == .notDetermined {
                            _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
                        } else {
                            openSettings()
                        }
                        await refresh()
                    }
                    Divider()
                    PermissionRow(title: L("perm_microphone"), granted: mic == .granted) {
                        if mic == .undetermined {
                            _ = await AVAudioApplication.requestRecordPermission()
                        } else {
                            openSettings()
                        }
                        await refresh()
                    }
                }
                Text(L("perm_focus_hint")).font(.footnote).foregroundStyle(Brand.secondaryText)
            }
            .padding(24)
        }
        .background(Brand.background)
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 10) {
                Button(L("continue_"), action: onNext).buttonStyle(PrimaryButtonStyle())
                if !(mic == .granted && notifications == .authorized) {
                    Button(L("calls_later"), action: onNext)
                }
            }
            .padding(24)
            .background(Brand.background)
        }
        .task { await refresh() }
        .onChange(of: scenePhase) { _, p in if p == .active { Task { await refresh() } } }
    }

    private func refresh() async {
        notifications = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        mic = AVAudioApplication.shared.recordPermission
    }

    private func openSettings() {
        if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
    }
}

struct PermissionRow: View {
    let title: String
    let granted: Bool
    let fix: () async -> Void
    var body: some View {
        HStack {
            Image(systemName: granted ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                .foregroundStyle(granted ? Brand.success : Brand.primary)
            Text(title)
            Spacer()
            if !granted {
                Button(L("perm_fix")) { Task { await fix() } }.buttonStyle(.borderedProminent)
            }
        }
    }
}

struct DoneView: View {
    @Environment(AppModel.self) private var model
    let cloud: Bool
    let onFinish: () -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                StepHeader(step: 4, title: L("try_it"), body_: L("done_subtitle"))
                Card {
                    if cloud {
                        StepRow(number: 1, title: L("try_cloud_1")) {
                            Text(L("try_cloud_1_body")).font(.subheadline).foregroundStyle(Brand.secondaryText)
                        }
                        StepRow(number: 2, title: L("try_cloud_2")) {
                            Text(L("try_cloud_2_body")).font(.subheadline).foregroundStyle(Brand.secondaryText)
                        }
                    } else {
                        StepRow(number: 1, title: L("try_self_1")) { CommandBox(command: "agentring msg \"hello\"") }
                        StepRow(number: 2, title: L("try_self_2")) { CommandBox(command: "agentring call \"test\"") }
                        StepRow(number: 3, title: L("try_self_3")) {
                            Text(L("try_self_3_body")).font(.subheadline).foregroundStyle(Brand.secondaryText)
                            CommandBox(command: "claude mcp add agentring -- agentring mcp")
                        }
                    }
                }
            }
            .padding(24)
        }
        .background(Brand.background)
        .navigationBarBackButtonHidden()
        .safeAreaInset(edge: .bottom) {
            Button(L("open_app"), action: onFinish).buttonStyle(PrimaryButtonStyle()).padding(24).background(Brand.background)
        }
    }
}
