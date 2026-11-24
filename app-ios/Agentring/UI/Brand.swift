import SwiftUI
import UIKit

/// The coral brand of the web and the Android app, in light and dark.
enum Brand {
    static let primary = dynamic(light: 0xC2410C, dark: 0xFF9A66)
    static let primaryContainer = dynamic(light: 0xFFE8DB, dark: 0x5A2207)
    static let onPrimaryContainer = dynamic(light: 0x7A2A06, dark: 0xFFDBC9)
    static let background = dynamic(light: 0xF7F6F2, dark: 0x15161A)
    static let surface = dynamic(light: 0xFFFFFF, dark: 0x1F2025)
    static let surfaceVariant = dynamic(light: 0xEFEDE6, dark: 0x2A2B31)
    static let secondaryText = dynamic(light: 0x4A4C55, dark: 0xC4C3CC)
    static let outline = dynamic(light: 0xC4C0B3, dark: 0x4A4C55)
    static let success = dynamic(light: 0x1F7A3A, dark: 0x6FD08C)
    static let danger = dynamic(light: 0xB42318, dark: 0xFF8A80)
    static let callGreen = Color(red: 0.20, green: 0.75, blue: 0.35)
    static let callRed = Color(red: 0.92, green: 0.23, blue: 0.20)

    private static func dynamic(light: UInt32, dark: UInt32) -> Color {
        Color(UIColor { $0.userInterfaceStyle == .dark ? UIColor(hex: dark) : UIColor(hex: light) })
    }
}

extension UIColor {
    convenience init(hex: UInt32) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255, blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }
}

/// A rounded card, as on the Android app.
struct Card<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 12) { content }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(16)
            .background(Brand.surface, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
    }
}

struct PrimaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .frame(maxWidth: .infinity, minHeight: 52)
            .foregroundStyle(.white)
            .background(Brand.primary.opacity(configuration.isPressed ? 0.8 : 1), in: Capsule())
    }
}

struct SecondaryButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .frame(maxWidth: .infinity, minHeight: 52)
            .foregroundStyle(Brand.primary)
            .background(Brand.primaryContainer.opacity(configuration.isPressed ? 0.7 : 1), in: Capsule())
    }
}

/// A numbered step in the pairing instructions.
struct StepRow<Detail: View>: View {
    let number: Int
    let title: String
    @ViewBuilder var detail: Detail
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Text("\(number)")
                .font(.subheadline.bold())
                .frame(width: 28, height: 28)
                .background(Brand.primaryContainer, in: Circle())
                .foregroundStyle(Brand.onPrimaryContainer)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.headline)
                detail
            }
            Spacer(minLength: 0)
        }
    }
}

/// Monospaced command with a copy button.
struct CommandBox: View {
    let command: String
    @State private var copied = false
    var body: some View {
        HStack(alignment: .top) {
            Text(command)
                .font(.system(.footnote, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button {
                UIPasteboard.general.string = command
                copied = true
            } label: {
                Image(systemName: copied ? "checkmark" : "doc.on.doc")
            }
            .accessibilityLabel(L("copy"))
        }
        .padding(12)
        .background(Brand.surfaceVariant, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
    }
}
