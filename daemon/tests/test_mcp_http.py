"""`agentring mcp-http` as an MCP client sees it: Streamable HTTP against a real relay."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from agentring.mcp_http import HttpConfig, McpHttpServer
from agentring.mcp_server import AgentringTools
from agentring.relay_client import RelayClient

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
    assert r.json()["result"]["protocolVersion"] == "2025-06-18"

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
