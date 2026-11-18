import XCTest

/// Drives the real app against a real relay: pair by pasting the code, reach Home, get a
/// message from the agent, reply, try to call an offline agent, get an incoming call.
/// scripts/test-ui.sh starts the relay and passes these (TEST_RUNNER_ prefix stripped):
///   AGENTRING_UI_RELAY, AGENTRING_UI_API_KEY, AGENTRING_UI_PAIRING_CODE
final class OnboardingFlowTests: XCTestCase {
    private var env: [String: String] { ProcessInfo.processInfo.environment }
    private lazy var relay = env["AGENTRING_UI_RELAY"] ?? ""
    private lazy var apiKey = env["AGENTRING_UI_API_KEY"] ?? ""

    private func agent(_ method: String, _ path: String, _ body: [String: Any]? = nil) throws -> Any? {
        var r = URLRequest(url: URL(string: relay + path)!)
        r.httpMethod = method
        r.setValue("Bearer \(apiKey)", forHTTPHeaderField: "Authorization")
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let done = expectation(description: path)
        var out: Data?
        URLSession.shared.dataTask(with: r) { data, _, _ in out = data; done.fulfill() }.resume()
        wait(for: [done], timeout: 10)
        return out.flatMap { try? JSONSerialization.jsonObject(with: $0) }
    }

    private func snap(_ name: String) {
        let a = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        a.name = name
        a.lifetime = .keepAlways
        add(a)
    }

    /// Notification / microphone permission prompts belong to SpringBoard.
    private func allowSystemAlerts() {
        let springboard = XCUIApplication(bundleIdentifier: "com.apple.springboard")
        for label in ["Allow", "Permitir", "OK", "Aceptar"] {
            let b = springboard.buttons[label]
            if b.waitForExistence(timeout: 2) { b.tap() }
        }
    }

    func testPairMessageAndCalls() throws {
        let code = try XCTUnwrap(env["AGENTRING_UI_PAIRING_CODE"], "run through scripts/test-ui.sh")
        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(en)", "-AppleLocale", "en_US"]
        app.launch()

        snap("1-welcome")
        app.buttons["Get started"].tap()
        snap("2-how")
        app.buttons.containing(NSPredicate(format: "label CONTAINS 'My own server'")).firstMatch.tap()
        snap("3-pair")
        app.buttons["Can't scan? Paste the code"].tap()
        let field = app.alerts.textFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.typeText(code)
        app.alerts.buttons["Pair"].tap()
        allowSystemAlerts()

        let paired = app.alerts.firstMatch
        XCTAssertTrue(paired.waitForExistence(timeout: 15), "no 'Paired with' alert")
        XCTAssertTrue(paired.label.contains("Claude"), paired.label)
        snap("4-paired")
        paired.buttons["Continue"].tap()

        snap("5-calls")
        app.buttons["Continue"].firstMatch.tap()
        snap("6-try")
        app.buttons["Open agentring"].tap()

        XCTAssertTrue(app.buttons["Call agent"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Connected"].waitForExistence(timeout: 15), "socket not connected")
        // The local relay has no APNs key: the app must not claim push works.
        XCTAssertTrue(app.staticTexts["Not configured on relay"].waitForExistence(timeout: 10))
        snap("7-home")

        // The agent writes; the message arrives over the socket.
        _ = try agent("POST", "/v1/messages", ["text": "Build finished ✅"])
        app.tabBars.buttons["Messages"].tap()
        XCTAssertTrue(app.staticTexts["Build finished ✅"].waitForExistence(timeout: 10))
        let input = app.textFields.firstMatch
        input.tap()
        input.typeText("Great, deploy it")
        app.buttons["Send"].tap()
        XCTAssertTrue(app.staticTexts["Great, deploy it"].waitForExistence(timeout: 10))
        snap("8-messages")
        let replies = try agent("GET", "/v1/messages?from=user") as? [String: Any]
        let texts = (replies?["messages"] as? [[String: Any]])?.compactMap { $0["text"] as? String } ?? []
        XCTAssertTrue(texts.contains("Great, deploy it"), "relay didn't get the reply: \(texts)")

        // No daemon is connected, so calling the agent must say it's offline.
        app.staticTexts["Build finished ✅"].tap() // tapping the thread closes the keyboard
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 5), "keyboard still up")
        app.tabBars.buttons["Home"].tap()
        app.buttons["Call agent"].tap()
        XCTAssertTrue(app.staticTexts["The agent is offline"].waitForExistence(timeout: 10))
        snap("9-offline")

        // The agent calls: CallKit shows the incoming call (system UI outside the app).
        _ = try agent("POST", "/v1/calls", ["reason": "Deploy to prod is ready"])
        sleep(3)
        snap("10-incoming")
    }
}
