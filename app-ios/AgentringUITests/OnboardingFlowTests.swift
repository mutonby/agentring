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

    /// "Call agent" with a real agent (Grok voice AI or the echo agent) that answers.
    private func callTheAgent(_ app: XCUIApplication, echo: Bool) throws {
        XCTAssertTrue(app.staticTexts["Online"].waitForExistence(timeout: 20), "agent not online")
        let hangUp = app.buttons["Hang up"]
        // The simulator's CallKit sometimes ends a call it just started (a CXEndCallAction from
        // the system ~40 ms after CXStartCallAction; never seen on a phone): try up to 3 times.
        var held = false
        for attempt in 1...3 where !held {
            app.buttons["Call agent"].tap()
            guard hangUp.waitForExistence(timeout: 10) else { continue }
            snap("9-calling-\(attempt)")
            // Active = the timer replaced "Calling…"/"Connecting…" and the mute button showed up.
            guard app.buttons["Mute"].waitForExistence(timeout: 30) else { continue }
            snap("10-active")
            sleep(8) // let the agent talk
            held = hangUp.exists
            if !held { _ = app.staticTexts["Call ended"].waitForNonExistence(timeout: 10) }
        }
        XCTAssertTrue(held, "no call stayed up for 8 s in 3 attempts")
        XCTAssertFalse(app.staticTexts["Couldn't connect the call"].exists)
        app.buttons["Mute"].tap()
        XCTAssertTrue(app.buttons["Mute"].isSelected, "mute didn't toggle")
        snap("11-muted")
        hangUp.tap()
        XCTAssertTrue(app.staticTexts["Call ended"].waitForExistence(timeout: 10))
        // The call screen lingers a moment over the tabs, then closes by itself.
        XCTAssertTrue(app.staticTexts["Call ended"].waitForNonExistence(timeout: 10), "call screen stuck")
        if echo {
            app.tabBars.buttons["Messages"].tap()
            // The agent reports after every call; the last report is the call that held.
            let reports = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'test agent heard'"))
            XCTAssertTrue(reports.firstMatch.waitForExistence(timeout: 20), "the echo agent didn't report")
            sleep(2)
            let n = reports.allElementsBoundByIndex.compactMap { Int($0.label.components(separatedBy: " ")[3]) }.max() ?? 0
            XCTAssertGreaterThan(n, 0, "the agent got no audio from the phone")
            app.tabBars.buttons["Home"].tap()
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

        app.staticTexts["Build finished ✅"].tap() // tapping the thread closes the keyboard
        XCTAssertTrue(app.keyboards.firstMatch.waitForNonExistence(timeout: 5), "keyboard still up")
        app.tabBars.buttons["Home"].tap()
        let agentKind = env["AGENTRING_UI_AGENT"] ?? "none"
        if agentKind == "none" {
            // No daemon is connected, so calling the agent must say it's offline.
            app.buttons["Call agent"].tap()
            XCTAssertTrue(app.staticTexts["The agent is offline"].waitForExistence(timeout: 10))
            snap("9-offline")
        } else {
            try callTheAgent(app, echo: agentKind == "echo")
        }

        // Group call: another phone answers an agent call; this one is offered "Join".
        let token = try XCTUnwrap(agent("POST", "/v1/pairing-tokens") as? [String: Any])
        let qr = try JSONSerialization.jsonObject(with: Data((token["qr_payload"] as! String).utf8)) as! [String: Any]
        var pair = URLRequest(url: URL(string: relay + "/v1/pair")!)
        pair.httpMethod = "POST"
        pair.setValue("application/json", forHTTPHeaderField: "Content-Type")
        pair.httpBody = try JSONSerialization.data(withJSONObject: ["pairing_token": qr["pairing_token"]!, "device_name": "Other phone", "platform": "android"])
        let other = try XCTUnwrap(fetch(pair) as? [String: Any])
        let otherJwt = try XCTUnwrap(other["device_jwt"] as? String)
        let placed = try XCTUnwrap(agent("POST", "/v1/calls", ["reason": "Group call"]) as? [String: Any])
        let callId = try XCTUnwrap((placed["call"] as? [String: Any])?["call_id"] as? String)
        var accept = URLRequest(url: URL(string: relay + "/v1/calls/\(callId)/accept")!)
        accept.httpMethod = "POST"
        accept.setValue("Bearer \(otherJwt)", forHTTPHeaderField: "Authorization")
        _ = try fetch(accept)
        XCTAssertTrue(app.buttons["Join"].waitForExistence(timeout: 15), "no Join offer for the call in progress")
        snap("12-join-offer")
        _ = try agent("POST", "/v1/calls/\(callId)/end")
        XCTAssertTrue(app.buttons["Join"].waitForNonExistence(timeout: 15), "Join offer stayed after the call ended")

        // The agent revokes this phone: the app says so and offers to pair again.
        let devices = try XCTUnwrap(agent("GET", "/v1/devices") as? [String: Any])
        let me = try XCTUnwrap((devices["devices"] as? [[String: Any]])?.first { $0["platform"] as? String == "ios" })
        _ = try agent("DELETE", "/v1/devices/\(me["device_id"] as! String)")
        XCTAssertTrue(app.staticTexts["This phone was unpaired"].waitForExistence(timeout: 20), "revocation not shown")
        XCTAssertTrue(app.buttons["Pair again"].exists)
        snap("13-revoked")
    }

    private func fetch(_ r: URLRequest) throws -> Any? {
        let done = expectation(description: r.url!.path)
        var out: Data?
        URLSession.shared.dataTask(with: r) { data, _, _ in out = data; done.fulfill() }.resume()
        wait(for: [done], timeout: 10)
        return out.flatMap { try? JSONSerialization.jsonObject(with: $0) }
    }
}
