// Runs the real NotificationService (and Shared/) on macOS, where UNNotificationServiceExtension
// also exists: a push signed like the relay signs it must get its real text; a tampered or
// foreign one must keep the generic text. Built and run by scripts/test-nse.sh.
import AgentringCore
import Foundation
import UserNotifications

var failures = 0
func check(_ ok: Bool, _ what: String) {
    print(ok ? "✔ \(what)" : "✘ \(what)")
    if !ok { failures += 1 }
}

let secret = Base64URL.encode(Data((0..<32).map { _ in UInt8.random(in: 0...255) }))
let relayId = "rly_nse_test"
let saved = PairingKeychain.save(Pairing(relayUrl: "https://relay.test", deviceId: "dev_1", deviceJwt: "jwt", pushSecret: secret,
                                         relayId: relayId, agentName: "Claude", canPush: true))
check(saved, "pairing saved to the keychain")

func push(_ event: String, sig: String? = nil, relay: String = relayId) -> UNNotificationContent {
    let c = UNMutableNotificationContent()
    c.title = "Claude"
    c.body = "New message" // what iOS shows from the loc-key when nothing changes it
    c.userInfo = ["v": "1", "relay_id": relay, "event": event, "sig": sig ?? PushVerifier.sign(pushSecret: secret, eventJSON: event)!]
    let req = UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil)
    var out: UNNotificationContent?
    NotificationService().didReceive(req) { out = $0 }
    return out ?? c
}

let msg = #"{"type":"msg.new","id":"evt_1","ts":1761200000000,"seq":5,"msg_id":"msg_1","from":"agent","text":"Deploy finished ✅"}"#
let ok = push(msg)
check(ok.body == "Deploy finished ✅", "signed msg.new shows its text (got \(ok.body))")
check(ok.userInfo["msg_id"] as? String == "msg_1", "msg_id handed to the Reply action")

let tampered = push(msg.replacingOccurrences(of: "Deploy", with: "Wire money"), sig: PushVerifier.sign(pushSecret: secret, eventJSON: msg))
check(tampered.body == "New message", "tampered event keeps the generic text (got \(tampered.body))")

let foreign = push(msg, relay: "rly_other")
check(foreign.body == "New message", "push from another relay keeps the generic text")

let missed = push(#"{"type":"call.missed","id":"evt_2","ts":1761200000000,"seq":6,"call_id":"call_9"}"#)
check(missed.body != "New message" && missed.userInfo["call_id"] as? String == "call_9", "missed call gets its text and call_id")

let inbox = SharedInbox.drain()
check(inbox.contains { $0.msgId == "msg_1" && $0.text == "Deploy finished ✅" }, "verified message handed to the app through the App Group inbox")

PairingKeychain.clear() // exit() skips defer: clean up the test item explicitly
print(failures == 0 ? "all NSE checks passed" : "\(failures) NSE check(s) failed")
exit(failures == 0 ? 0 : 1)
