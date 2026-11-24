import AgentringCore
import SwiftUI
import UIKit

struct RootView: View {
    @Environment(AppModel.self) private var model
    @Environment(CallController.self) private var calls

    var body: some View {
        ZStack {
            Brand.background.ignoresSafeArea()
            if model.pairing == nil || !model.onboarded {
                OnboardingFlow()
            } else if model.revoked {
                RevokedView()
            } else {
                MainTabs()
            }
        }
        .fullScreenCover(isPresented: Binding(get: { calls.phase != nil && calls.phase != .incoming }, set: { _ in })) {
            CallView()
        }
    }
}

struct MainTabs: View {
    @Environment(AppModel.self) private var model
    var body: some View {
        @Bindable var model = model
        TabView(selection: $model.selectedTab) {
            NavigationStack { HomeView() }
                .tabItem { Label(L("tab_home"), systemImage: "house.fill") }
                .tag(AppModel.Tab.home)
            NavigationStack { MessagesView() }
                .tabItem { Label(L("tab_messages"), systemImage: "message.fill") }
                .badge(model.messages.unreadFromAgent().count)
                .tag(AppModel.Tab.messages)
            NavigationStack { SettingsView() }
                .tabItem { Label(L("tab_settings"), systemImage: "gearshape.fill") }
                .tag(AppModel.Tab.settings)
        }
    }
}

// MARK: - Home

struct HomeView: View {
    @Environment(AppModel.self) private var model
    @Environment(CallController.self) private var calls
    @State private var callError: String?
    @State private var placing = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let offer = calls.joinOffer {
                    Card {
                        Label(String(format: L("call_in_progress_with"), offer.agentName), systemImage: "phone.connection.fill")
                            .font(.headline)
                        Button(L("call_join")) { calls.join(callId: offer.callId) }.buttonStyle(PrimaryButtonStyle())
                    }
                }
                Card {
                    HStack(spacing: 12) {
                        ZStack {
                            Circle().fill(Brand.primaryContainer).frame(width: 52, height: 52)
                            Text(String(model.agentName.prefix(1))).font(.title2.bold()).foregroundStyle(Brand.onPrimaryContainer)
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            Text(model.agentName).font(.title3.bold())
                            presence
                        }
                    }
                    Text(String(format: L("call_agent_hint"), model.agentName)).foregroundStyle(Brand.secondaryText)
                    Button {
                        placing = true
                        Task {
                            callError = await calls.callAgent()
                            placing = false
                        }
                    } label: {
                        if placing { ProgressView().tint(.white) } else { Label(L("call_agent"), systemImage: "phone.fill") }
                    }
                    .buttonStyle(PrimaryButtonStyle())
                    .disabled(placing)
                    if let callError {
                        Text(callError).font(.subheadline).foregroundStyle(Brand.danger)
                    }
                }
                Card {
                    Text(L("connection")).font(.headline)
                    StatusRow(title: L("status_relay"), value: relayText, ok: model.connection == .connected)
                    StatusRow(title: L("status_agent"), value: agentText, ok: model.agentOnline == true)
                    StatusRow(title: L("status_push"), value: pushText, ok: model.pushState == .registered)
                }
            }
            .padding(16)
        }
        .background(Brand.background)
        .navigationTitle("agentring")
        .refreshable { await model.refresh() }
    }

    private var presence: some View {
        HStack(spacing: 6) {
            Circle().fill(model.agentOnline == true ? Brand.success : Brand.outline).frame(width: 8, height: 8)
            Text(agentText).font(.subheadline).foregroundStyle(Brand.secondaryText)
        }
    }

    private var relayText: String {
        switch model.connection {
        case .connected: L("connected")
        case .connecting: L("connecting")
        case .offline: L("offline")
        }
    }

    private var agentText: String {
        switch model.agentOnline {
        case true?: L("agent_online")
        case false?: L("agent_offline")
        case nil: L("agent_unknown")
        }
    }

    private var pushText: String {
        switch model.pushState {
        case .registered: L("push_registered")
        case .registering: L("push_registering")
        case .notConfigured: L("push_not_configured")
        case .unknown: L("unknown")
        case .error(let e): String(format: L("push_error"), e)
        }
    }
}

struct StatusRow: View {
    let title: String
    let value: String
    let ok: Bool
    var body: some View {
        HStack {
            Text(title)
            Spacer()
            Text(value).foregroundStyle(ok ? Brand.success : Brand.secondaryText)
        }
        .font(.subheadline)
    }
}

// MARK: - Messages

struct MessagesView: View {
    @Environment(AppModel.self) private var model
    @State private var draft = ""
    @State private var sending = false
    @State private var error: String?
    @FocusState private var focused: Bool

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 8) {
                    if model.messages.messages.isEmpty {
                        Text(String(format: L("messages_empty"), model.agentName))
                            .multilineTextAlignment(.center).foregroundStyle(Brand.secondaryText).padding(32)
                    }
                    ForEach(model.messages.messages) { m in
                        Bubble(message: m, agentName: model.agentName).id(m.msgId)
                    }
                }
                .padding(16)
            }
            .scrollDismissesKeyboard(.interactively)
            // Tapping the thread closes the keyboard (it covers the tab bar).
            .contentShape(Rectangle())
            .simultaneousGesture(TapGesture().onEnded { focused = false })
            .onAppear {
                model.isShowingMessages = true
                model.markAgentMessagesRead()
                if let last = model.messages.messages.last { proxy.scrollTo(last.msgId, anchor: .bottom) }
            }
            .onDisappear { model.isShowingMessages = false }
            .onChange(of: model.messages.messages.count) { _, _ in
                model.markAgentMessagesRead()
                if let last = model.messages.messages.last { withAnimation { proxy.scrollTo(last.msgId, anchor: .bottom) } }
            }
        }
        .background(Brand.background)
        .navigationTitle(L("tab_messages"))
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 4) {
                if let error { Text(error).font(.footnote).foregroundStyle(Brand.danger) }
                HStack(alignment: .bottom, spacing: 8) {
                    TextField(String(format: L("message_hint"), model.agentName), text: $draft, axis: .vertical)
                        .lineLimit(1...5)
                        .focused($focused)
                        .padding(.horizontal, 14).padding(.vertical, 10)
                        .background(Brand.surface, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
                    Button {
                        send()
                    } label: {
                        Image(systemName: "arrow.up.circle.fill").font(.system(size: 34))
                    }
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || sending)
                    .accessibilityLabel(L("send"))
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
            .background(.bar)
        }
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        sending = true
        error = nil
        Task {
            if let e = await model.send(text) {
                error = e
            } else {
                draft = ""
            }
            sending = false
        }
    }
}

struct Bubble: View {
    let message: Message
    let agentName: String
    var body: some View {
        let mine = !message.isFromAgent
        HStack {
            if mine { Spacer(minLength: 48) }
            VStack(alignment: mine ? .trailing : .leading, spacing: 4) {
                Text(message.text)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14).padding(.vertical, 10)
                    .foregroundStyle(mine ? .white : .primary)
                    .background(mine ? Brand.primary : Brand.surface, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
                HStack(spacing: 4) {
                    Text(Date(timeIntervalSince1970: Double(message.createdAt) / 1000), style: .time)
                    if mine && message.readAt != nil { Text("· " + L("read")) }
                }
                .font(.caption2).foregroundStyle(Brand.secondaryText)
            }
            if !mine { Spacer(minLength: 48) }
        }
    }
}

// MARK: - Settings

struct SettingsView: View {
    @Environment(AppModel.self) private var model
    @State private var confirmUnpair = false

    var body: some View {
        List {
            Section {
                LabeledContent(L("settings_relay"), value: model.pairing?.relayUrl ?? "")
                LabeledContent(L("settings_agent"), value: model.agentName)
                LabeledContent(L("settings_device"), value: model.pairing?.deviceId ?? "")
                    .textSelection(.enabled)
            }
            Section {
                Button(L("test_notification")) { model.sendTestNotification() }
            }
            Section {
                Button(L("unpair"), role: .destructive) { confirmUnpair = true }
            }
            Section {
                LabeledContent("Version", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "")
            }
        }
        .navigationTitle(L("tab_settings"))
        .confirmationDialog(String(format: L("unpair_confirm"), model.pairing?.deviceId ?? ""), isPresented: $confirmUnpair, titleVisibility: .visible) {
            Button(L("unpair"), role: .destructive) { model.unpair() }
            Button(L("cancel"), role: .cancel) {}
        }
    }
}

struct RevokedView: View {
    @Environment(AppModel.self) private var model
    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "iphone.slash").font(.system(size: 56)).foregroundStyle(Brand.primary)
            Text(L("revoked_title")).font(.title2.bold())
            Text(L("revoked_body")).multilineTextAlignment(.center).foregroundStyle(Brand.secondaryText)
            Button(L("pair_again")) { model.unpair() }.buttonStyle(PrimaryButtonStyle())
        }
        .padding(32)
    }
}

// MARK: - Call screen (in the app; the lock screen uses CallKit's)

struct CallView: View {
    @Environment(CallController.self) private var calls

    var body: some View {
        VStack(spacing: 24) {
            Spacer()
            ZStack {
                Circle().fill(Brand.primaryContainer).frame(width: 120, height: 120)
                Text(String((calls.info?.agentName ?? "A").prefix(1))).font(.system(size: 52, weight: .bold)).foregroundStyle(Brand.onPrimaryContainer)
            }
            if calls.info?.urgent == true {
                Text(L("call_urgent")).font(.caption.bold()).padding(.horizontal, 10).padding(.vertical, 4)
                    .background(Brand.danger, in: Capsule()).foregroundStyle(.white)
            }
            Text(calls.info?.agentName ?? "").font(.largeTitle.bold())
            if let reason = calls.info?.reason, !reason.isEmpty {
                Text(reason).multilineTextAlignment(.center).foregroundStyle(Brand.secondaryText).padding(.horizontal, 32)
            }
            statusLine.font(.headline).foregroundStyle(Brand.secondaryText)
            Spacer()
            if case .active = calls.phase {
                HStack(spacing: 40) {
                    RoundButton(icon: calls.muted ? "mic.slash.fill" : "mic.fill", label: L("call_mute"), on: calls.muted) {
                        calls.requestMute(!calls.muted)
                    }
                    RoundButton(icon: "speaker.wave.2.fill", label: L("call_speaker"), on: calls.speaker) { calls.toggleSpeaker() }
                }
            }
            if calls.phase?.isLive == true {
                Button { calls.requestHangUp() } label: {
                    Image(systemName: "phone.down.fill").font(.title).foregroundStyle(.white)
                        .frame(width: 76, height: 76).background(Brand.callRed, in: Circle())
                }
                .accessibilityLabel(L("call_hang_up"))
            }
            Spacer().frame(height: 32)
        }
        .frame(maxWidth: .infinity)
        .background(Brand.background.ignoresSafeArea())
    }

    @ViewBuilder private var statusLine: some View {
        switch calls.phase {
        case .outgoing: Text(String(format: L("call_calling"), calls.info?.agentName ?? ""))
        case .connecting: Text(L("call_connecting"))
        case .active(let since):
            if calls.reconnecting { Text(L("call_reconnecting")) } else { Text(since, style: .timer).monospacedDigit() }
        case .ended(let message): Text(message)
        case .incoming: Text(L("call_incoming"))
        case nil: EmptyView()
        }
    }
}

struct RoundButton: View {
    let icon: String
    let label: String
    let on: Bool
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Image(systemName: icon).font(.title2)
                    .frame(width: 64, height: 64)
                    .foregroundStyle(on ? Brand.background : .primary)
                    .background(on ? Color.primary : Brand.surfaceVariant, in: Circle())
                Text(label).font(.caption)
            }
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}
