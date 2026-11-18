import AgentringCore
import SwiftUI
import UIKit
import UserNotifications

@main
struct AgentringApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @Environment(\.scenePhase) private var scenePhase

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(AppModel.shared)
                .environment(CallController.shared)
                .tint(Brand.primary)
        }
        .onChange(of: scenePhase) { _, phase in
            switch phase {
            case .active: AppModel.shared.becameActive()
            case .background: AppModel.shared.enteredBackground()
            default: break
            }
        }
    }
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        // Order matters: the audio gate before any LiveKit room, PushKit before the
        // launch finishes (a VoIP push may be what launched the app).
        AudioGate.configure()
        _ = CallController.shared
        AppModel.shared.start()
        PushRegistrar.shared.start()
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushRegistrar.shared.didRegister(apnsToken: deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        AppModel.shared.pushState = .error(error.localizedDescription)
    }

    /// Silent pushes (`call.end`): catch up so a stale "join" offer disappears.
    func application(_ application: UIApplication, didReceiveRemoteNotification userInfo: [AnyHashable: Any]) async -> UIBackgroundFetchResult {
        guard let event = PushContent.verifiedEvent(userInfo, pairing: AppModel.shared.pairing) else { return .noData }
        AppModel.shared.onEvent(event)
        if event.callId != nil {
            let center = UNUserNotificationCenter.current()
            let delivered = await center.deliveredNotifications()
            let ids = delivered.filter { $0.request.content.userInfo["call_id"] as? String == event.callId
                && $0.request.content.categoryIdentifier == "CALL_JOINABLE" }.map(\.request.identifier)
            center.removeDeliveredNotifications(withIdentifiers: ids)
        }
        return .newData
    }
}

/// Localized string from Localizable.strings.
func L(_ key: String) -> String {
    NSLocalizedString(key, comment: "")
}
