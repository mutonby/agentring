// swift-tools-version:5.10
import PackageDescription

// Pure Swift mirror of protocol/relay.openapi.yaml plus the relay client. No UIKit,
// so `swift test` runs on a Mac (scripts/test-core.sh works without Xcode).
let package = Package(
    name: "AgentringCore",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "AgentringCore", targets: ["AgentringCore"])],
    targets: [
        .target(name: "AgentringCore"),
        .testTarget(name: "AgentringCoreTests", dependencies: ["AgentringCore"]),
    ]
)
