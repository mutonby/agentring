import Foundation

/// Resumable event stream: reconnects with exponential backoff and resumes from the
/// last `seq` the caller has processed. With `expectedRelayId`, a server whose
/// `ws.hello` names another relay is dropped and nothing it sends is delivered.
public final class RelaySocket: NSObject, URLSessionWebSocketDelegate, @unchecked Sendable {
    public enum State: Sendable { case connecting, connected, disconnected }

    public struct Handlers: Sendable {
        public var onEvent: @Sendable (Event) -> Void
        public var onState: @Sendable (State) -> Void
        /// Credentials rejected (revoked device). The socket stops reconnecting.
        public var onUnauthorized: @Sendable () -> Void
        /// The server is not the relay we paired with. The socket stops reconnecting.
        public var onWrongRelay: @Sendable (String) -> Void

        public init(
            onEvent: @escaping @Sendable (Event) -> Void,
            onState: @escaping @Sendable (State) -> Void = { _ in },
            onUnauthorized: @escaping @Sendable () -> Void = {},
            onWrongRelay: @escaping @Sendable (String) -> Void = { _ in }
        ) {
            self.onEvent = onEvent
            self.onState = onState
            self.onUnauthorized = onUnauthorized
            self.onWrongRelay = onWrongRelay
        }
    }

    private let api: RelayAPI
    private let lastSeq: @Sendable () -> Int64
    private let handlers: Handlers
    private let expectedRelayId: String?
    private let queue = DispatchQueue(label: "dev.agentring.relay-socket")
    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private var retry: DispatchWorkItem?
    private var pinger: DispatchSourceTimer?
    private var attempts = 0
    private var running = false
    private var wrongRelay = false

    public init(api: RelayAPI, expectedRelayId: String?, lastSeq: @escaping @Sendable () -> Int64, handlers: Handlers) {
        self.api = api
        self.expectedRelayId = expectedRelayId
        self.lastSeq = lastSeq
        self.handlers = handlers
        super.init()
        let q = OperationQueue()
        q.underlyingQueue = queue
        q.maxConcurrentOperationCount = 1
        session = URLSession(configuration: .default, delegate: self, delegateQueue: q)
    }

    public func start() {
        queue.async {
            guard !self.running else { return }
            self.running = true
            self.wrongRelay = false
            self.connect()
        }
    }

    /// Closes the connection and stops reconnecting. `start()` may be called again.
    public func stop() {
        queue.async {
            self.running = false
            self.retry?.cancel()
            self.stopPinger()
            self.task?.cancel(with: .normalClosure, reason: nil)
            self.task = nil
            self.handlers.onState(.disconnected)
        }
    }

    /// Reconnects at once (e.g. the network came back or the app returned to the foreground).
    public func kick() {
        queue.async {
            guard self.running, self.task == nil else { return }
            self.retry?.cancel()
            self.attempts = 0
            self.connect()
        }
    }

    private func connect() {
        guard running else { return }
        handlers.onState(.connecting)
        guard let req = try? api.webSocketRequest(afterSeq: lastSeq()) else {
            running = false
            handlers.onState(.disconnected)
            return
        }
        let t = session.webSocketTask(with: req)
        task = t
        t.resume()
        receive(on: t)
    }

    private func receive(on t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self else { return }
            self.queue.async {
                guard self.task === t else { return }
                switch result {
                case .failure:
                    self.onDown(t, unauthorized: (t.response as? HTTPURLResponse)?.statusCode == 401)
                case .success(let message):
                    if case .string(let text) = message { self.deliver(text, from: t) }
                    if self.task === t { self.receive(on: t) }
                }
            }
        }
    }

    private func deliver(_ text: String, from t: URLSessionWebSocketTask) {
        guard !wrongRelay, let event = try? Event.parse(text) else { return }
        if case .wsHello(let hello) = event.body, let expected = expectedRelayId, hello.relayId != expected {
            wrongRelay = true
            running = false
            task = nil
            stopPinger()
            t.cancel(with: .normalClosure, reason: nil)
            handlers.onState(.disconnected)
            handlers.onWrongRelay(hello.relayId)
            return
        }
        handlers.onEvent(event)
    }

    public func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didOpenWithProtocol protocol: String?) {
        guard task === webSocketTask else { return }
        attempts = 0
        startPinger(webSocketTask)
        handlers.onState(.connected)
    }

    public func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask, didCloseWith closeCode: URLSessionWebSocketTask.CloseCode, reason: Data?) {
        onDown(webSocketTask, unauthorized: closeCode.rawValue == 4401)
    }

    private func onDown(_ t: URLSessionWebSocketTask, unauthorized: Bool) {
        guard task === t else { return }
        task = nil
        stopPinger()
        handlers.onState(.disconnected)
        if unauthorized {
            running = false
            handlers.onUnauthorized()
            return
        }
        guard running else { return }
        let base = min(30.0, pow(2.0, Double(min(attempts, 5))))
        attempts += 1
        let delay = base * Double.random(in: 0.8...1.2)
        let item = DispatchWorkItem { [weak self] in self?.connect() }
        retry = item
        queue.asyncAfter(deadline: .now() + delay, execute: item)
    }

    /// Detects half-open connections (iOS keeps a dead socket "open" after a network change).
    private func startPinger(_ t: URLSessionWebSocketTask) {
        stopPinger()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 20, repeating: 20)
        timer.setEventHandler { [weak self, weak t] in
            guard let self, let t, self.task === t else { return }
            t.sendPing { error in
                guard error != nil else { return }
                self.queue.async { self.onDown(t, unauthorized: false) }
            }
        }
        timer.resume()
        pinger = timer
    }

    private func stopPinger() {
        pinger?.cancel()
        pinger = nil
    }
}
