"""`agentring mcp-http` as an MCP client sees it: Streamable HTTP against a real relay."""

from __future__ import annotations

import asyncio
import json
import socket
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from agentring import mcp_http
from agentring.mcp_http import MAX_BATCH, MAX_SESSIONS_PER_TENANT, HttpConfig, HttpError, KeyResolver, McpHttpServer, Tenant, UpstreamError
from agentring.mcp_server import AgentringTools
from agentring.relay_client import RelayClient
from agentring.store import Store

from .conftest import API_KEY
from .test_e2e import simulate_phone_pairing

ACCEPT = "application/json, text/event-stream"


class ServerThread:
    """McpHttpServer on its own event loop in a thread, on a free port."""

    def __init__(self, cfg: HttpConfig):
        self.cfg = cfg
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.loop.run_until_complete, args=(self._main(),), daemon=True)
        self.thread.start()
        assert self.ready.wait(10)

    async def _main(self) -> None:
        self.stop = asyncio.Event()
        self.server = McpHttpServer(self.cfg)
        host, port = await self.server.start()
        self.url = f"http://{host}:{port}"
        self.ready.set()
        await self.stop.wait()
        await self.server.aclose()

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.stop.set)
        self.thread.join(10)


@pytest.fixture()
def start(tmp_path: Path):
    started: list[ServerThread] = []

    def _start(**kw) -> ServerThread:
        kw.setdefault("data_dir", tmp_path / "mcp-data")
        kw.setdefault("port", 0)
        s = ServerThread(HttpConfig(**kw))
        started.append(s)
        return s

    yield _start
    for s in started:
        s.close()


def sse_messages(text: str) -> list[dict]:
    return [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]


class Client:
    def __init__(self, url: str, key: str = API_KEY):
        self.http = httpx.Client(base_url=url, timeout=60)
        self.headers = {"authorization": f"Bearer {key}", "accept": ACCEPT, "content-type": "application/json"}
        self.sid: str | None = None
        self.n = 0

    def post(self, msg, **headers) -> httpx.Response:
        h = {**self.headers, **({"mcp-session-id": self.sid} if self.sid else {}), **headers}
        return self.http.post("/mcp", content=json.dumps(msg), headers=h)

    def request(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        r = self.post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}})
        assert r.status_code == 200, r.text
        if r.headers["content-type"].startswith("text/event-stream"):
            msgs = sse_messages(r.text)
            assert len(msgs) == 1, r.text
            msg = msgs[0]
        else:
            msg = r.json()
        assert msg["id"] == self.n
        return msg

    def initialize(self) -> dict:
        self.n += 1
        r = self.post({"jsonrpc": "2.0", "id": self.n, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}}})
        assert r.status_code == 200, r.text
        self.sid = r.headers["mcp-session-id"]
        assert self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code == 202
        return r.json()["result"]

    def tool(self, name: str, **args) -> dict:
        return self.request("tools/call", {"name": name, "arguments": args})["result"]


def test_handshake_sessions_and_http_errors(relay: str, start):
    s = start(relay_url=relay)
    c = Client(s.url)
    assert httpx.get(f"{s.url}/healthz").json() == {"ok": True}

    init = c.initialize()
    assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "agentring"
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    assert c.request("ping")["result"] == {}
    names = [t["name"] for t in c.request("tools/list")["result"]["tools"]]
    assert names == ["send_message", "get_replies", "call", "status"]
    assert c.request("nope/method")["error"]["code"] == -32601
    assert c.request("tools/call", {"name": "nope"})["error"]["code"] == -32602

    # An older client gets its version back; an unknown one gets ours.
    r = c.post({"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert r.json()["result"]["protocolVersion"] == "2024-11-05"
    r = c.post({"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert r.json()["result"]["protocolVersion"] == "2025-11-25"
    # Hermes speaks 2025-11-25 and sends it in the header.
    r = c.post({"jsonrpc": "2.0", "id": 98, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}},
               **{"mcp-protocol-version": "2025-11-25"})
    assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == "2025-11-25"

    # Batches (2025-03-26) get an array back.
    r = c.post([{"jsonrpc": "2.0", "id": "a", "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/x"},
                {"jsonrpc": "2.0", "id": "b", "method": "tools/list"}])
    assert [m["id"] for m in r.json()] == ["a", "b"]

    assert c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, **{"mcp-protocol-version": "1999-01-01"}).status_code == 400
    assert c.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, **{"origin": "http://evil.example"}).status_code == 403
    assert c.http.post("/mcp", content="{nope", headers=c.headers).json()["error"]["code"] == -32700
    assert c.http.get("/mcp", headers=c.headers).status_code == 405
    assert c.http.get("/mcp", headers=c.headers).headers["allow"] == "POST, DELETE"
    assert c.http.get("/elsewhere").status_code == 404

    # Without a session header it works statelessly.
    assert Client(s.url).request("ping")["result"] == {}

    # DELETE ends the session; the id is then unknown (404: initialize again).
    assert c.http.delete("/mcp", headers={**c.headers, "mcp-session-id": c.sid}).status_code == 204
    assert c.post({"jsonrpc": "2.0", "id": 5, "method": "ping"}).status_code == 404
    assert c.http.delete("/mcp", headers={**c.headers, "mcp-session-id": "nope"}).status_code == 404


def test_auth_failures(relay: str, start):
    s = start(relay_url=relay)
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    for headers, error in [({}, None), ({"authorization": "Basic Zm9vOmJhcg=="}, None),
                           ({"authorization": "Bearer not-a-key"}, "invalid_token"),
                           ({"authorization": "Bearer ak_wrong_key"}, "invalid_token")]:
        r = httpx.post(f"{s.url}/mcp", content=ping, headers={"content-type": "application/json", **headers})
        assert r.status_code == 401, headers
        assert r.headers["www-authenticate"].startswith('Bearer realm="agentring"')
        assert (f'error="{error}"' in r.headers["www-authenticate"]) == (error is not None)
        assert r.json()["error"]["code"] == -32001
    # A session is tied to its key.
    c = Client(s.url)
    c.initialize()
    other = Client(s.url, key="ak_wrong_key")
    other.sid = c.sid
    assert other.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_body_limit(relay: str, start):
    s = start(relay_url=relay, max_body=200)
    c = Client(s.url)
    r = c.post({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * 300}})
    assert r.status_code == 413
    assert c.request("ping")["result"] == {}  # a new connection works


def test_messages_and_replies_end_to_end(relay: str, start, tmp_path: Path):
    with RelayClient(relay, API_KEY) as rc:
        phone = simulate_phone_pairing(rc.create_pairing_token()["qr_payload"])
    s = start(relay_url=relay, poll_s=0.3)
    c = Client(s.url)
    c.initialize()

    st = c.tool("status")
    assert not st["isError"] and st["structuredContent"]["phones"][0]["name"] == "Test Phone"
    assert st["structuredContent"]["relay"] == relay

    sent = c.tool("send_message", text="¿Despliego?")
    msg_id = sent["structuredContent"]["msg_id"]
    assert json.loads(sent["content"][0]["text"]) == {"msg_id": msg_id}
    assert c.tool("get_replies")["structuredContent"] == {"replies": []}
    assert c.tool("send_message")["isError"]  # missing text

    def phone_replies_later():
        time.sleep(1)
        httpx.post(f"{relay}/v1/messages", headers=phone["headers"], json={"text": "sí", "reply_to": msg_id})

    threading.Thread(target=phone_replies_later).start()
    t0 = time.time()
    c.n += 1
    r = c.post({"jsonrpc": "2.0", "id": c.n, "method": "tools/call",
                "params": {"name": "get_replies", "arguments": {"wait_s": 20}}})
    assert r.headers["content-type"] == "text/event-stream"  # a waiting tool call streams
    got = sse_messages(r.text)[0]["result"]["structuredContent"]["replies"]
    assert [(m["text"], m["reply_to"]) for m in got] == [("sí", msg_id)]
    assert time.time() - t0 < 15
    assert c.tool("get_replies")["structuredContent"] == {"replies": []}  # each reply once

    # The state is per tenant, on disk: another session (or a restart) does not see it again.
    dbs = list((tmp_path / "mcp-data" / "tenants").glob("relay_rly_*.db"))
    assert len(dbs) == 1
    s.close()
    s2 = start(relay_url=relay)
    c2 = Client(s2.url)
    assert c2.tool("get_replies")["structuredContent"] == {"replies": []}
    httpx.post(f"{relay}/v1/messages", headers=phone["headers"], json={"text": "otra"})
    assert [m["text"] for m in c2.tool("get_replies")["structuredContent"]["replies"]] == ["otra"]


# ------------------------------------------------------------------ hub mode

TENANT = "tnt_01JABCDEFGHJKMNPQRSTVWXYZ0"
ADMIN_TOKEN = "admin-token-" + "x" * 32


class FakeAdmin:
    """The hub's admin API, resolve-key only."""

    def __init__(self, relay: str):
        self.hits = 0
        self.auth: list[str] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.hits += 1
                outer.auth.append(self.headers.get("authorization", ""))
                if self.path != "/admin/v1/resolve-key" or self.headers.get("authorization") != f"Bearer {ADMIN_TOKEN}":
                    code, out = 401, {"error": {"code": "unauthorized", "message": "no"}}
                elif body.get("api_key") == API_KEY:
                    code, out = 200, {"tenant_id": TENANT, "base_url": relay}
                else:
                    code, out = 404, {"error": {"code": "not_found", "message": "unknown key"}}
                data = json.dumps(out).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


def test_hub_mode_resolves_keys_through_the_admin_api(relay: str, start, tmp_path: Path, monkeypatch):
    admin = FakeAdmin(relay)
    try:
        token_file = tmp_path / "admin-token"
        token_file.write_text(ADMIN_TOKEN + "\n")
        monkeypatch.setenv("AGENTRING_HUB_ADMIN_URL", admin.url)
        monkeypatch.setenv("AGENTRING_HUB_ADMIN_TOKEN_FILE", str(token_file))
        monkeypatch.delenv("AGENTRING_RELAY_URL", raising=False)
        cfg = HttpConfig.from_env(port=0, data_dir=tmp_path / "hub-data")
        assert cfg.hub and cfg.admin_token == ADMIN_TOKEN and cfg.relay_url is None
        s = ServerThread(cfg)
        try:
            c = Client(s.url)
            c.initialize()
            st = c.tool("status")["structuredContent"]
            assert st["relay"] == relay and st["relay_status"]["relay_id"].startswith("rly_")
            assert c.request("ping")["result"] == {}
            assert admin.hits == 1  # cached by key hash
            assert admin.auth == [f"Bearer {ADMIN_TOKEN}"]
            assert (tmp_path / "hub-data" / "tenants" / f"{TENANT}.db").exists()

            r = Client(s.url, key="ak_unknown").post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert r.status_code == 401 and 'error="invalid_token"' in r.headers["www-authenticate"]
            Client(s.url, key="ak_unknown").post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert admin.hits == 3  # unknown keys are not cached
        finally:
            s.close()

        # A wrong admin token is our problem, not the client's: 503, not 401.
        bad = ServerThread(HttpConfig(port=0, admin_url=admin.url, admin_token="wrong" * 8, data_dir=tmp_path / "bad"))
        try:
            r = Client(bad.url).post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert r.status_code == 503 and r.headers["retry-after"]
        finally:
            bad.close()
    finally:
        admin.close()


def test_relay_client_keeps_the_tenant_path_prefix():
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json={"relay_id": "rly_x", "events": [], "has_more": False})

    with RelayClient(f"https://hub.example/t/{TENANT}/", "ak_x", transport=httpx.MockTransport(handler)) as c:
        c.status()
        c.list_events(after_seq=3)
        assert c.ws_url(7) == f"wss://hub.example/t/{TENANT}/v1/ws?after_seq=7"
    assert seen == [f"/t/{TENANT}/v1/status", f"/t/{TENANT}/v1/events"]


def test_config_from_env(monkeypatch, tmp_path: Path):
    from agentring.config import ConfigError

    for v in ("AGENTRING_RELAY_URL", "AGENTRING_HUB_ADMIN_URL", "AGENTRING_HUB_ADMIN_TOKEN", "AGENTRING_HUB_ADMIN_TOKEN_FILE",
              "AGENTRING_MCP_PORT", "AGENTRING_MCP_MAX_CALLS", "AGENTRING_MCP_ALLOWED_ORIGINS"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(ConfigError):
        HttpConfig.from_env()
    monkeypatch.setenv("AGENTRING_HUB_ADMIN_URL", "http://relay:8080/")
    with pytest.raises(ConfigError, match="admin token"):
        HttpConfig.from_env()
    monkeypatch.setenv("AGENTRING_HUB_ADMIN_TOKEN", "t" * 40)
    monkeypatch.setenv("AGENTRING_MCP_MAX_CALLS", "9")
    monkeypatch.setenv("AGENTRING_MCP_ALLOWED_ORIGINS", "https://a.example, https://b.example/")
    cfg = HttpConfig.from_env(port=1234, data_dir=tmp_path)
    assert (cfg.admin_url, cfg.port, cfg.max_calls, cfg.host) == ("http://relay:8080", 1234, 9, "127.0.0.1")
    assert cfg.allowed_origins == ("https://a.example", "https://b.example")
    monkeypatch.setenv("AGENTRING_MCP_PORT", "nope")
    with pytest.raises(ConfigError, match="AGENTRING_MCP_PORT"):
        HttpConfig.from_env()


# ------------------------------------------------------------------ calls


def test_slow_call_streams_keepalives(relay: str, start, monkeypatch):
    async def slow_call(self, reason, context=None, urgent=False):
        await asyncio.sleep(1.2)
        return {"call_id": "call_test", "answered": True, "reason": reason}

    monkeypatch.setattr(AgentringTools, "call", slow_call)
    s = start(relay_url=relay, keepalive_s=0.2)
    c = Client(s.url)
    c.initialize()
    body = json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                       "params": {"name": "call", "arguments": {"reason": "test"}}})
    lines: list[str] = []
    with c.http.stream("POST", "/mcp", content=body, headers={**c.headers, "mcp-session-id": c.sid}) as r:
        assert r.status_code == 200 and r.headers["content-type"] == "text/event-stream"
        for line in r.iter_lines():
            lines.append(line)
    assert lines.count(": keep-alive") >= 3
    data = [json.loads(x[5:]) for x in lines if x.startswith("data:")]
    assert data[0]["id"] == 7 and data[0]["result"]["structuredContent"]["call_id"] == "call_test"
    assert not data[0]["result"]["isError"]

    # A plain JSON client (no text/event-stream in Accept) waits for a JSON answer.
    r = c.post(json.loads(body), accept="application/json")
    assert r.headers["content-type"] == "application/json" and r.json()["result"]["structuredContent"]["answered"]


def test_cancelled_request_ends_the_stream(relay: str, start, monkeypatch):
    started = threading.Event()

    async def endless_call(self, reason, context=None, urgent=False):
        started.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(AgentringTools, "call", endless_call)
    s = start(relay_url=relay, keepalive_s=0.2)
    c = Client(s.url)
    c.initialize()

    def cancel_soon():
        assert started.wait(10)
        c2 = Client(s.url)
        c2.sid = c.sid
        r = c2.post({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 8}})
        assert r.status_code == 202

    threading.Thread(target=cancel_soon).start()
    t0 = time.time()
    r = c.post({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "call", "arguments": {"reason": "x"}}})
    assert time.time() - t0 < 10
    assert r.status_code == 200 and sse_messages(r.text) == []  # no response for a cancelled request


def test_call_cap_answers_busy(relay: str, start, monkeypatch):
    pytest.importorskip("livekit")
    monkeypatch.setenv("AGENTRING_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-never-used")
    s = start(relay_url=relay, max_calls=0)
    c = Client(s.url)
    res = c.tool("call", reason="test")
    assert res["isError"] and res["content"][0]["text"].startswith("busy")


def test_bad_ids_and_long_keys(relay: str, start):
    s = start(relay_url=relay)
    c = Client(s.url)
    c.initialize()
    # Lists/objects are not JSON-RPC ids: refused, and the server keeps working.
    for bad in ([1], {"a": 1}, True):
        r = c.post({"jsonrpc": "2.0", "id": bad, "method": "tools/call", "params": {"name": "status", "arguments": {}}})
        assert r.status_code == 400, r.text
    r = c.post({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": [1]}})
    assert r.status_code == 202
    assert c.request("ping")["result"] == {}
    assert s.server.pool._open[next(iter(s.server.pool._open))].active == 0  # no leaked slot
    # Keys longer than the hub accepts are a 401, not a 503.
    long = Client(s.url, key="ak_" + "x" * 200)
    assert long.post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_stdio_refuses_bad_ids():
    from agentring.mcp_server import McpServer

    sent: list[dict] = []
    srv = McpServer(None, lambda line: sent.append(json.loads(line)))  # type: ignore[arg-type]
    asyncio.run(srv.handle(json.dumps({"jsonrpc": "2.0", "id": [1], "method": "tools/call"})))
    asyncio.run(srv.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": {}}})))
    assert sent == [{"jsonrpc": "2.0", "id": None, "error": {"code": -32600,
                                                            "message": "invalid request: id must be a string or a number"}}]


def test_cancelled_get_replies_does_not_swallow_a_reply(relay: str, tmp_path: Path):
    from agentring.mcp_server import AgentringTools
    from agentring.store import Store

    with RelayClient(relay, API_KEY) as rc:
        phone = simulate_phone_pairing(rc.create_pairing_token()["qr_payload"])
        httpx.post(f"{relay}/v1/messages", headers=phone["headers"], json={"text": "reply"})
        tools = AgentringTools(RelayClient(relay, API_KEY), Store(tmp_path / "s.db"), agents_file=None)
        cancelled = threading.Event()
        cancelled.set()  # the request was cancelled while the thread was fetching
        assert tools._new_replies(cancelled) == []
        assert [r["text"] for r in asyncio.run(tools.get_replies())["replies"]] == ["reply"]
        tools.close()


# ------------------------------------------------------------------ strict framing

NOWHERE = "http://127.0.0.1:9"  # these requests never get as far as the relay


def connect(url: str, *, rcvbuf: int | None = None) -> socket.socket:
    u = urlsplit(url)
    s = socket.socket()
    if rcvbuf:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
    s.settimeout(5)
    s.connect((u.hostname, u.port))
    return s


def raw(url: str, data: bytes) -> bytes:
    """Sends bytes as they are and returns everything until the server closes."""
    with connect(url) as s:
        s.sendall(data)
        out = b""
        while chunk := s.recv(65536):
            out += chunk
    return out


def with_length(value: bytes) -> bytes:
    return b"POST /mcp HTTP/1.1\r\nhost: x\r\ncontent-length: " + value + b"\r\n\r\n{}GET /healthz HTTP/1.1\r\nhost: x\r\n\r\n"


def chunked(chunks: bytes, te: bytes = b"chunked") -> bytes:
    return (b"POST /mcp HTTP/1.1\r\nhost: x\r\ntransfer-encoding: " + te + b"\r\n\r\n" + chunks
            + b"GET /healthz HTTP/1.1\r\nhost: x\r\n\r\n")


@pytest.mark.parametrize("request_bytes", [
    with_length(b"+2"), with_length(b"0_2"), with_length(b"2\x0b"), with_length(b"\xa02"), with_length(b"2\x85"),
    with_length(b"0x2"), with_length(b"-2"), with_length(b"2,2"), with_length(b"2 2"), with_length(b"\xb2"),
    with_length(b"9" * 5000),
    chunked(b"0x2\r\n{}\r\n0\r\n\r\n"), chunked(b"+2\r\n{}\r\n0\r\n\r\n"), chunked(b"0_2\r\n{}\r\n0\r\n\r\n"),
    chunked(b"2 \r\n{}\r\n0\r\n\r\n"), chunked(b" 2\r\n{}\r\n0\r\n\r\n"), chunked(b"-1\r\n{}\r\n0\r\n\r\n"),
    chunked(b"2\n;x\r\n{}\r\n0\r\n\r\n"),
    chunked(b"-0\r\n\r\n"), chunked(b"+0\r\n\r\n"), chunked(b"0x0\r\n\r\n"),
    chunked(b"2\r\n{}\r\n0\r\n\r\n", te=b"chunked\x0b"), chunked(b"2\r\n{}\r\n0\r\n\r\n", te=b"chunked\xa0"),
    # A value with a bare LF: a proxy that ends lines there would see another header.
    b"GET /healthz HTTP/1.1\r\nhost: x\r\nx-a: 1\ncontent-length: 5\r\n\r\n",
])
def test_bad_framing_is_a_400_and_nothing_after_it_is_read(start, request_bytes):
    out = raw(start(relay_url=NOWHERE).url, request_bytes)
    assert out.startswith(b"HTTP/1.1 400 ") and out.count(b"HTTP/1.1 ") == 1


@pytest.mark.parametrize("request_bytes", [
    b"POST /mcp HTTP/1.1\r\nhost: x\r\ncontent-length:   2\t\r\nconnection: close\r\n\r\n{}",  # OWS is fine
    b"POST /mcp HTTP/1.1\r\nhost: x\r\ncontent-length: 002\r\nconnection: close\r\n\r\n{}",
    b"POST /mcp HTTP/1.1\r\nhost: x\r\ntransfer-encoding: Chunked\r\nconnection: close\r\n\r\n2\r\n{}\r\n0\r\n\r\n",
    b"POST /mcp HTTP/1.1\r\nhost: x\r\ntransfer-encoding: chunked\r\nconnection: close\r\n\r\n"
    b"1;a=b\r\n{\r\n01 ;c\r\n}\r\n000\r\ntrailer: 1\r\n\r\n",
])
def test_good_framing_still_reaches_the_server(start, request_bytes):
    assert raw(start(relay_url=NOWHERE).url, request_bytes).startswith(b"HTTP/1.1 401 ")  # read whole, then no key


# ------------------------------------------------------------------ resource limits


def ping(n: int) -> dict:
    return {"jsonrpc": "2.0", "id": n, "method": "ping"}


def wait_replies(n: int, wait_s: int = 60) -> dict:
    return {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": "get_replies", "arguments": {"wait_s": wait_s}}}


def until(check, timeout: float = 8.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def post_raw(url: str, msg, *, accept: str = "application/json", key: str = API_KEY) -> socket.socket:
    """Sends a POST and leaves the connection open, unread."""
    body = json.dumps(msg).encode()
    s = connect(url)
    s.sendall(f"POST /mcp HTTP/1.1\r\nhost: x\r\nauthorization: Bearer {key}\r\naccept: {accept}\r\n"
              f"content-type: application/json\r\ncontent-length: {len(body)}\r\n\r\n".encode() + body)
    return s


def test_batch_size_limit(relay: str, start):
    c = Client(start(relay_url=relay).url)
    ok = c.post([ping(i) for i in range(1, MAX_BATCH + 1)])
    assert ok.status_code == 200 and len(ok.json()) == MAX_BATCH
    over = c.post([ping(i) for i in range(1, MAX_BATCH + 2)])
    assert over.status_code == 400 and "at most" in over.json()["error"]["message"]


def test_tool_calls_in_flight_are_capped_and_released(relay: str, start):
    s = start(relay_url=relay, max_inflight=2, poll_s=0.1)
    c = Client(s.url)
    waiting = [post_raw(s.url, wait_replies(i)) for i in (1, 2)]
    assert until(lambda: s.server.inflight.get(next(iter(s.server.inflight), "")) == 2)

    r = c.post(wait_replies(3, 0))
    assert r.status_code == 429 and r.headers["retry-after"] == "5" and "too many" in r.json()["error"]["message"]
    assert c.post(ping(4)).status_code == 200  # only tool calls count
    assert c.post([ping(i) for i in range(5, 5 + MAX_BATCH)]).status_code == 200

    # A plain JSON request stops when its client hangs up, and frees its place.
    for w in waiting:
        w.close()
    assert until(lambda: not s.server.inflight)
    assert not c.tool("status")["isError"]
    assert c.post([wait_replies(i, 0) for i in (7, 8, 9)]).status_code == 429  # a batch counts each call
    assert not s.server.inflight


def test_finished_tool_calls_release_their_place(relay: str, start):
    s = start(relay_url=relay, max_inflight=1)
    c = Client(s.url)
    for _ in range(5):
        assert not c.tool("status")["isError"]
    assert until(lambda: not s.server.inflight)


def test_hanging_up_leaves_a_phone_call_running(relay: str, start, monkeypatch):
    done = threading.Event()

    async def call(self, reason, context=None, urgent=False):
        await asyncio.sleep(1.5)
        done.set()
        return {"answered": True}

    monkeypatch.setattr(AgentringTools, "call", call)
    monkeypatch.setattr(mcp_http, "DISCONNECT_POLL_S", 0.05)
    s = start(relay_url=relay)
    post_raw(s.url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "call", "arguments": {"reason": "x"}}}).close()
    assert done.wait(8)
    assert until(lambda: not s.server.inflight)


def test_connection_caps(relay: str, start):
    s = start(relay_url=relay, max_connections=2)
    held = [connect(s.url), connect(s.url)]
    assert until(lambda: len(s.server._conns) == 2)
    with connect(s.url) as refused:  # answered and closed without reading anything
        out = refused.recv(1024)
        assert out.startswith(b"HTTP/1.1 503 ") and b"retry-after: 5" in out
        assert refused.recv(1024) == b""
    assert len(s.server._conns) == 2
    for h in held:
        h.close()
    assert until(lambda: not s.server._conns)
    assert raw(s.url, b"GET /healthz HTTP/1.1\r\nhost: x\r\nconnection: close\r\n\r\n").startswith(b"HTTP/1.1 200 ")


def test_open_requests_per_tenant_are_capped_and_released(relay: str, start):
    s = start(relay_url=relay, max_tenant_connections=1, poll_s=0.1)
    c = Client(s.url)
    assert c.post(ping(1)).status_code == 200  # one at a time is fine, on and on
    assert c.post(ping(2)).status_code == 200
    w = post_raw(s.url, wait_replies(3), accept=ACCEPT)
    assert until(lambda: s.server.tenant_conns)
    r = c.post(ping(4))
    assert r.status_code == 429 and r.headers["retry-after"] == "5"
    w.close()
    assert until(lambda: not s.server.tenant_conns)
    assert c.post(ping(5)).status_code == 200


def big_catalog(monkeypatch, size: int) -> None:
    monkeypatch.setattr("agentring.mcp_server.TOOLS", [{"name": "x", "description": "d" * size, "inputSchema": {}}])


def test_response_size_limit(relay: str, start, monkeypatch):
    big_catalog(monkeypatch, 5000)
    s = start(relay_url=relay, max_response=2000)
    r = Client(s.url).post({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 500 and r.json()["error"]["message"] == "response too large"


def test_streamed_response_size_limit(relay: str, start, monkeypatch):
    async def call(self, reason, context=None, urgent=False):
        return {"transcript": "x" * 5000}

    monkeypatch.setattr(AgentringTools, "call", call)
    s = start(relay_url=relay, max_response=2000)
    r = Client(s.url).post({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                            "params": {"name": "call", "arguments": {"reason": "x"}}})
    assert sse_messages(r.text) == [{"jsonrpc": "2.0", "id": 7, "error": {"code": -32603, "message": "response too large"}}]


def test_a_client_that_stops_reading_is_cut_off_and_its_buffer_freed(relay: str, start, monkeypatch):
    big_catalog(monkeypatch, 16 * 1024 * 1024)
    s = start(relay_url=relay, write_timeout_s=0.3, max_response=64 * 1024 * 1024)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    stalled = connect(s.url, rcvbuf=4096)
    stalled.sendall(f"POST /mcp HTTP/1.1\r\nhost: x\r\nauthorization: Bearer {API_KEY}\r\n"
                    f"content-type: application/json\r\ncontent-length: {len(body)}\r\n\r\n".encode() + body)
    assert until(lambda: len(s.server._conns) == 1)
    assert until(lambda: not s.server._conns), "the connection task must end although nobody reads"
    got = 0
    try:
        while chunk := stalled.recv(1 << 20):
            got += len(chunk)
    except OSError:
        pass
    stalled.close()
    assert got < 16 * 1024 * 1024  # aborted, not flushed


def test_tenant_lock_is_not_held_during_relay_requests():
    tools: AgentringTools

    class Relay:
        relay_url = NOWHERE
        held: list[bool] = []

        def _note(self):
            self.held.append(tools._db.locked())

        def status(self):
            self._note()
            return {"relay_id": "rly_x"}

        def list_events(self, after_seq=0):
            self._note()
            first = after_seq == 0
            return {"has_more": False, "events": [
                {"type": "msg.new", "seq": 1, "ts": 1, "msg_id": "msg_1", "from": "user", "text": "hi"}] if first else []}

        def mark_read(self, msg_id):
            self._note()

    relay = Relay()
    tools = AgentringTools(relay, Store(":memory:"), agents_file=None)
    assert [r["text"] for r in tools._new_replies()] == ["hi"]
    assert tools._new_replies() == []  # each reply once
    assert len(relay.held) >= 5 and not any(relay.held)


def test_concurrent_get_replies_return_each_reply_once(tmp_path: Path):
    n = 40

    class Relay:
        relay_url = NOWHERE

        def status(self):
            return {"relay_id": "rly_x"}

        def list_events(self, after_seq=0):
            time.sleep(0.001)
            return {"has_more": False, "events": [
                {"type": "msg.new", "seq": i, "ts": i, "msg_id": f"msg_{i:03}", "from": "user", "text": str(i)}
                for i in range(after_seq + 1, n + 1)]}

        def mark_read(self, msg_id):
            pass

    tools = AgentringTools(Relay(), Store(tmp_path / "s.db"), agents_file=None)
    got: list[str] = []
    threads = [threading.Thread(target=lambda: got.extend(r["text"] for r in tools._new_replies())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(got, key=int) == [str(i) for i in range(1, n + 1)]
    tools.store.close()


# ------------------------------------------------------------------ sessions


@pytest.fixture()
def server(tmp_path: Path):
    srv = McpHttpServer(HttpConfig(relay_url=NOWHERE, data_dir=tmp_path / "mcp-data"))
    yield srv
    asyncio.run(srv.resolver.aclose())


A, B = Tenant("tnt_A", NOWHERE), Tenant("tnt_B", NOWHERE)


def test_a_tenant_at_its_session_cap_only_evicts_its_own(server: McpHttpServer):
    theirs = server._new_session(B, "v")
    mine = [server._new_session(A, "v") for _ in range(MAX_SESSIONS_PER_TENANT + 50)]
    assert theirs.sid in server.sessions
    left = [s for s in server.sessions.values() if s.tenant_id == "tnt_A"]
    assert len(left) == MAX_SESSIONS_PER_TENANT
    assert {s.sid for s in left} == {s.sid for s in mine[-MAX_SESSIONS_PER_TENANT:]}  # the oldest went


def test_a_full_table_never_evicts_another_tenants_live_session(server: McpHttpServer, monkeypatch):
    monkeypatch.setattr(mcp_http, "MAX_SESSIONS", 3)
    theirs = [server._new_session(B, "v"), server._new_session(B, "v"), server._new_session(A, "v")]
    assert server._new_session(A, "v").tenant_id == "tnt_A"  # replaces its own
    assert theirs[0].sid in server.sessions and theirs[1].sid in server.sessions and theirs[2].sid not in server.sessions
    with pytest.raises(HttpError) as e:
        server._new_session(Tenant("tnt_C", NOWHERE), "v")  # holds nothing to replace
    assert e.value.status == 429
    theirs[0].last_seen -= mcp_http.SESSION_IDLE_S + 1  # expired sessions make room, whoever had them
    assert server._new_session(Tenant("tnt_C", NOWHERE), "v").sid in server.sessions
    assert theirs[0].sid not in server.sessions


def test_running_sessions_are_never_evicted(server: McpHttpServer, monkeypatch):
    monkeypatch.setattr(mcp_http, "MAX_SESSIONS_PER_TENANT", 2)
    busy = [server._new_session(A, "v") for _ in range(2)]
    for s in busy:
        s.running[1] = object()
    with pytest.raises(HttpError) as e:
        server._new_session(A, "v")
    assert e.value.status == 429 and all(s.sid in server.sessions for s in busy)


# ------------------------------------------------------------------ a deleted tenant's mirror


def hub_cfg(tmp_path: Path) -> HttpConfig:
    return HttpConfig(admin_url="http://hub:8080", admin_token="t" * 40, data_dir=tmp_path / "mcp-data")


def hub_server(tmp_path: Path, answer) -> McpHttpServer:
    cfg = hub_cfg(tmp_path)
    return McpHttpServer(cfg, resolver=KeyResolver(cfg, transport=httpx.MockTransport(answer)))


def not_found(what: str = "Tenant") -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "not_found", "message": f"{what} not found"}})


def mirror(server: McpHttpServer, tenant_id: str) -> Path:
    db = server.pool.dir / f"{tenant_id}.db"
    s = Store(db)
    s.upsert_message("msg_1", "user", "secret", 1)
    s.close()
    db.with_name(db.name + "-wal").write_bytes(b"x")
    db.with_name(db.name + "-shm").write_bytes(b"x")
    return db


def files_of(db: Path) -> list[str]:
    return sorted(p.name for p in db.parent.iterdir() if p.name.startswith(db.stem))


@pytest.mark.parametrize("reply", [
    httpx.Response(500, text="boom"),
    httpx.Response(502, text="bad gateway"),
    httpx.Response(503, json={"error": {"code": "unavailable", "message": "Tenant not found"}}),
    httpx.Response(401, json={"error": {"code": "unauthorized", "message": "no"}}),
    httpx.Response(404, text="<html>nginx: not found</html>"),  # a wrong admin URL
    httpx.Response(404, text=""),
    not_found("Route"),  # the admin API turning away a proxied request
])
def test_nothing_is_deleted_without_a_definite_answer(tmp_path: Path, reply):
    srv = hub_server(tmp_path, lambda req: reply)
    db = mirror(srv, "tnt_A")

    with pytest.raises(UpstreamError):
        asyncio.run(srv.resolver.tenant_gone("tnt_A"))
    asyncio.run(srv._sweep())
    assert db.exists() and len(files_of(db)) == 3
    srv.pool.close_all()


def test_nothing_is_deleted_when_the_hub_is_unreachable(tmp_path: Path):
    def down(req):
        raise httpx.ConnectError("no route")

    srv = hub_server(tmp_path, down)
    db = mirror(srv, "tnt_A")
    asyncio.run(srv._sweep())
    assert db.exists()


def test_a_tenant_that_exists_keeps_its_mirror(tmp_path: Path):
    hits: list[str] = []

    def answer(req: httpx.Request) -> httpx.Response:
        hits.append(f"{req.url.path} {req.headers['authorization']}")
        return httpx.Response(200, json={"tenant_id": "tnt_A", "disabled": True})

    srv = hub_server(tmp_path, answer)
    db = mirror(srv, "tnt_A")
    asyncio.run(srv._sweep())
    assert db.exists()
    assert hits == [f"/admin/v1/tenants/tnt_A Bearer {'t' * 40}"]


def test_sweep_deletes_only_what_the_hub_says_is_gone_and_stops_at_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mcp_http, "SWEEP_PACE_S", 0)
    replies = {"tnt_A": not_found(), "tnt_B": httpx.Response(200, json={}), "tnt_C": httpx.Response(500),
               "tnt_D": not_found()}
    srv = hub_server(tmp_path, lambda req: replies[req.url.path.rsplit("/", 1)[1]])
    dbs = {t: mirror(srv, t) for t in replies}
    (srv.pool.dir / "not a tenant.db").write_bytes(b"x")
    (srv.pool.dir / "notes.txt").write_bytes(b"x")
    asyncio.run(srv._sweep())
    assert files_of(dbs["tnt_A"]) == []  # the -wal and -shm too
    assert dbs["tnt_B"].exists()
    assert dbs["tnt_C"].exists()
    assert dbs["tnt_D"].exists()  # the 500 ended this sweep, D waits for the next
    assert (srv.pool.dir / "not a tenant.db").exists() and (srv.pool.dir / "notes.txt").exists()


def test_reap_closes_the_pooled_store_first_and_waits_for_requests(tmp_path: Path):
    srv = hub_server(tmp_path, lambda req: not_found())
    tenant = Tenant("tnt_A", "http://127.0.0.1:9/t/tnt_A")
    srv.pool.acquire(tenant, "ak_x")
    db = srv.pool.dir / "tnt_A.db"
    assert db.exists()
    srv._new_session(tenant, "v")

    assert asyncio.run(srv._reap("tnt_A")) is False  # a request is using it
    assert db.exists() and "tnt_A" in srv.pool._open

    store = srv.pool._open["tnt_A"].tools.store
    srv.pool.release("tnt_A", "ak_x")
    assert asyncio.run(srv._reap("tnt_A")) is True
    assert files_of(db) == [] and srv.pool._open == {} and srv.sessions == {}
    with pytest.raises(Exception, match="closed"):
        store.last_seq


def test_new_limit_settings_from_env(monkeypatch, tmp_path: Path):
    for v in ("AGENTRING_HUB_ADMIN_URL", "AGENTRING_HUB_ADMIN_TOKEN", "AGENTRING_HUB_ADMIN_TOKEN_FILE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AGENTRING_RELAY_URL", "http://relay:8080")
    monkeypatch.setenv("AGENTRING_MCP_MAX_INFLIGHT", "3")
    monkeypatch.setenv("AGENTRING_MCP_MAX_CONNECTIONS", "9")
    monkeypatch.setenv("AGENTRING_MCP_MAX_TENANT_CONNECTIONS", "4")
    monkeypatch.setenv("AGENTRING_MCP_SWEEP_S", "0")
    cfg = HttpConfig.from_env()
    assert (cfg.max_inflight, cfg.max_connections, cfg.max_tenant_connections, cfg.sweep_s) == (3, 9, 4, 0.0)


def test_mcp_http_refuses_a_public_http_relay(monkeypatch):
    from agentring.config import ConfigError

    for v in ("AGENTRING_HUB_ADMIN_URL", "AGENTRING_HUB_ADMIN_TOKEN", "AGENTRING_HUB_ADMIN_TOKEN_FILE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("AGENTRING_RELAY_URL", "http://relay.example.com")
    with pytest.raises(ConfigError, match="AGENTRING_RELAY_URL: refusing"):
        HttpConfig.from_env()


def test_hub_base_url_on_public_http_is_not_used(tmp_path: Path):
    def answer(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tenant_id": "tnt_A", "base_url": "http://relay.example.com/t/tnt_A"})

    r = KeyResolver(hub_cfg(tmp_path), transport=httpx.MockTransport(answer))
    with pytest.raises(UpstreamError):
        asyncio.run(r.resolve("ak_x"))
