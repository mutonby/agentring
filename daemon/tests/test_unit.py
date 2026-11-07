from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import typer
import websockets
from typer.testing import CliRunner

from agentring.cli import app, parse_since
from agentring.config import ConfigError, load_settings
from agentring.relay_client import (ALLOW_INSECURE_ENV, InsecureRelayError, RelayClient, RelayError, check_relay_url,
                                    is_private_host)
from agentring.store import Store
from agentring.sync import apply_event, catch_up, run_ws

from .conftest import API_KEY

NOW = 1_761_200_000_000


def test_parse_since():
    assert parse_since("10m", NOW) == NOW - 600_000
    assert parse_since("2h", NOW) == NOW - 7_200_000
    assert parse_since("1d", NOW) == NOW - 86_400_000
    assert parse_since("1761200000000", NOW) == 1_761_200_000_000
    assert parse_since("2025-10-23T06:13:20Z", NOW) == 1_761_200_000_000
    with pytest.raises(typer.BadParameter):
        parse_since("yesterday", NOW)


def test_apply_event_is_idempotent_and_tracks_seq():
    s = Store(":memory:")
    ev = {"type": "msg.new", "id": "evt_1", "ts": NOW, "seq": 3, "msg_id": "msg_A", "from": "user", "text": "hi"}
    apply_event(s, ev)
    apply_event(s, ev)
    assert [r["text"] for r in s.replies()] == ["hi"]
    assert s.last_seq == 3
    apply_event(s, {"type": "msg.read", "id": "evt_2", "ts": NOW + 1, "seq": 4, "msg_id": "msg_A", "by": "agent"})
    assert s.replies()[0]["read_at"] == NOW + 1
    apply_event(s, {"type": "agent.presence", "id": "evt_3", "ts": NOW, "online": True})
    assert s.last_seq == 4


def test_relay_change_resets_cursor():
    s = Store(":memory:")
    assert s.reset_if_relay_changed("rly_A") is False
    s.advance_seq(10)
    assert s.reset_if_relay_changed("rly_A") is False
    assert s.last_seq == 10
    assert s.reset_if_relay_changed("rly_B") is True
    assert s.last_seq == 0


def test_undelivered_replies():
    s = Store(":memory:")
    s.upsert_message("msg_1", "user", "a", NOW)
    s.upsert_message("msg_2", "agent", "b", NOW)
    s.upsert_message("msg_3", "user", "c", NOW + 5)
    assert [r["text"] for r in s.replies(undelivered_only=True)] == ["a", "c"]
    s.mark_delivered(["msg_1"], NOW)
    assert [r["text"] for r in s.replies(undelivered_only=True)] == ["c"]
    assert [r["text"] for r in s.replies(since=NOW + 1)] == ["c"]


def test_create_call_sends_device_ids_only_when_given():
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(201, json={"call": {}, "join": {}})

    with RelayClient("https://relay.test", "ak_x", transport=httpx.MockTransport(handler)) as c:
        c.create_call("all")
        c.create_call("one", device_ids=["dev_A"])
    assert bodies == [
        {"reason": "all", "priority": "normal"},
        {"reason": "one", "priority": "normal", "device_ids": ["dev_A"]},
    ]


# ------------------------------------------------------------------ bad events must not stop sync


def _msg(seq: int, text: str, reply_to: str | None = None, msg_id: str | None = None) -> str:
    """A msg.new as the relay sends it: JSON.stringify escapes a lone surrogate as \\ud83d."""
    return json.dumps({"type": "msg.new", "ts": NOW, "seq": seq, "msg_id": msg_id or f"msg_{seq}", "from": "user",
                       "text": text, **({"reply_to": reply_to} if reply_to else {})})


class _EventsClient:
    def __init__(self, events: list[str]):
        self.events = [json.loads(e) for e in events]

    def status(self) -> dict:
        return {"relay_id": "rly_A"}

    def list_events(self, after_seq: int) -> dict:
        return {"events": [e for e in self.events if e["seq"] > after_seq], "has_more": False}


def test_lone_surrogate_is_stored_as_replacement_char_and_sync_goes_on():
    s = Store(":memory:")
    client = _EventsClient([_msg(1, "half \ud83d emoji"), _msg(2, "a reply", reply_to="msg_\ud800"), _msg(3, "ok \U0001f600")])
    assert catch_up(client, s) == 3
    assert s.last_seq == 3
    rows = s.replies()
    assert [r["text"] for r in rows] == ["half \ufffd emoji", "a reply", "ok \U0001f600"]  # a whole emoji stays
    assert rows[1]["reply_to"] == "msg_\ufffd"


@pytest.mark.parametrize("event", [
    {"type": "msg.new", "seq": 1},  # no fields
    {"type": "msg.new", "seq": 1, "ts": NOW, "msg_id": "msg_\ud800", "from": "user", "text": "x"},  # surrogate in the id
])
def test_event_we_cannot_store_is_skipped_and_the_cursor_moves(event):
    s = Store(":memory:")
    apply_event(s, event)
    assert s.last_seq == 1 and s.replies() == []


def test_database_errors_do_not_move_the_cursor():
    class Locked(Store):
        def upsert_message(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

    s = Locked(":memory:")
    with pytest.raises(sqlite3.OperationalError):
        apply_event(s, json.loads(_msg(7, "hi")))
    assert s.last_seq == 0  # the message is fetched again on the next sync


def test_run_ws_survives_a_lone_surrogate():
    async def handler(ws):
        await ws.send(_msg(1, "half \ud83d"))
        await ws.send(_msg(2, "next"))
        await ws.wait_closed()

    async def run():
        server = await websockets.serve(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        store, stop = Store(":memory:"), asyncio.Event()
        with RelayClient(f"http://127.0.0.1:{port}", "ak_x") as client:
            await asyncio.wait_for(run_ws(client, "ak_x", store, lambda e: e["seq"] == 2 and stop.set(), stop=stop), 10)
        server.close()
        return store

    s = asyncio.run(run())
    assert [r["text"] for r in s.replies()] == ["half \ufffd", "next"] and s.last_seq == 2


# ------------------------------------------------------------------ the mirror is private


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


@pytest.fixture()
def loose_umask():
    old = os.umask(0o022)
    yield
    os.umask(old)


def test_store_creates_private_files(tmp_path: Path, loose_umask):
    home = tmp_path / "new-home"
    s = Store(home / "agentring.db")
    s.upsert_message("msg_1", "user", "secret", NOW)  # WAL: -wal and -shm exist from here on
    assert _mode(home) == 0o700
    files = [home / n for n in ("agentring.db", "agentring.db-wal", "agentring.db-shm")]
    assert [_mode(f) for f in files] == [0o600] * 3
    s.close()


def test_environment_only_setup_leaves_nothing_readable_by_others(relay: str, agentring_home, monkeypatch, loose_umask):
    monkeypatch.setenv("AGENTRING_RELAY_URL", relay)
    monkeypatch.setenv("AGENTRING_API_KEY", API_KEY)
    assert CliRunner().invoke(app, ["msg", "hi"]).exit_code == 0  # no `init`
    assert _mode(agentring_home) == 0o700
    files = list(agentring_home.iterdir())
    assert files and all(_mode(f) & 0o077 == 0 for f in files)


def test_store_tightens_an_existing_database_but_not_a_shared_directory(tmp_path: Path, loose_umask):
    db = tmp_path / "old.db"
    Store(db).close()
    db.chmod(0o644)
    tmp_path.chmod(0o755)
    Store(db).close()
    assert _mode(db) == 0o600
    assert _mode(tmp_path) == 0o755


# ------------------------------------------------------------------ logs


def test_verbose_does_not_log_websocket_headers():
    assert CliRunner().invoke(app, ["-v", "version"]).exit_code == 0
    assert not logging.getLogger("websockets.client").isEnabledFor(logging.DEBUG)


# ------------------------------------------------------------------ cleartext


@pytest.mark.parametrize("host", [
    "localhost", "LOCALHOST", "app.localhost", "127.0.0.1", "127.8.0.1", "::1", "::ffff:192.168.1.2", "0.0.0.0",
    "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.20", "169.254.1.1", "100.64.0.1", "100.127.255.254",
    "fd7a:115c:a1e0::1", "fe80::1", "nas.local", "nas.local.", "relay", "my_relay-1", "1relay",
])
def test_private_hosts(host):
    assert is_private_host(host)


@pytest.mark.parametrize("host", [
    "relay.example.com", "example.com.", "8.8.8.8", "172.32.0.1", "192.169.0.1", "100.128.0.1", "100.63.255.255",
    "2606:4700::1111", "::ffff:8.8.8.8", "localhost.example.com", "x.local.example.com",
    "134744072", "0x08080808", "127.1", "010.0.0.1.example",
])
def test_public_hosts(host):
    assert not is_private_host(host)


def test_public_http_is_refused_before_any_request():
    hits: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(req)
        return httpx.Response(200, json={})

    with pytest.raises(InsecureRelayError, match=ALLOW_INSECURE_ENV):
        RelayClient("http://relay.example.com", "ak_x", transport=httpx.MockTransport(handler))
    with pytest.raises(InsecureRelayError):
        RelayClient("HTTP://user@relay.example.com:8080/t/x", "ak_x", transport=httpx.MockTransport(handler))
    assert hits == []
    check_relay_url("https://relay.example.com")
    check_relay_url("http://relay:8080")


def test_opt_in_allows_public_http(monkeypatch):
    monkeypatch.setenv(ALLOW_INSECURE_ENV, "1")
    with RelayClient("http://relay.example.com", "ak_x"):
        pass


def test_environment_settings_get_the_same_rule(agentring_home, monkeypatch):
    monkeypatch.setenv("AGENTRING_RELAY_URL", "http://relay.example.com")
    monkeypatch.setenv("AGENTRING_API_KEY", "ak_x")
    with pytest.raises(ConfigError, match="plain http"):
        load_settings()
    monkeypatch.setenv("AGENTRING_RELAY_URL", "http://relay:8080")
    assert load_settings().relay_url == "http://relay:8080"


def test_init_refuses_public_http(agentring_home):
    r = CliRunner().invoke(app, ["init", "--relay", "http://relay.example.com", "--api-key", "ak_x"])
    assert r.exit_code == 1 and "plain http" in r.output
    assert not (agentring_home / "config.yaml").exists()


class _Reply(BaseHTTPRequestHandler):
    status, headers_out, body = 200, {}, b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(self.status)
        for k, v in self.headers_out.items():
            self.send_header(k, v)
        self.send_header("content-length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


@pytest.mark.parametrize("status, headers, body", [
    (308, {"location": "https://relay.example.com/v1/status"}, b""),  # the redirect that used to save the URL
    (302, {"location": "https://relay.example.com/"}, b"<html>moved</html>"),
    (200, {"content-type": "text/html"}, b"<html>a login page</html>"),
    (200, {}, b""),
    (204, {}, b""),
    (200, {"content-type": "application/json"}, b"[1, 2]"),
    (200, {"content-type": "application/json"}, b"{}"),
])
def test_init_saves_nothing_unless_a_relay_answered(agentring_home, status, headers, body):
    handler = type("H", (_Reply,), {"status": status, "headers_out": headers, "body": body})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        r = CliRunner().invoke(app, ["init", "--relay", f"http://127.0.0.1:{srv.server_address[1]}", "--api-key", "ak_x"])
    finally:
        srv.shutdown()
    assert r.exit_code == 1, r.output
    assert isinstance(r.exception, SystemExit)  # a clean error, not a traceback
    assert not (agentring_home / "config.yaml").exists() and not (agentring_home / ".env").exists()


def test_redirects_and_non_json_are_relay_errors():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/status":
            return httpx.Response(308, headers={"location": "https://other.example/v1/status"})
        return httpx.Response(200, text="not json")

    with RelayClient("https://relay.example.com", "ak_x", transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(RelayError) as e:
            c.status()
        assert (e.value.status, e.value.code) == (308, "redirect") and "https://other.example" in str(e.value)
        with pytest.raises(RelayError) as e:
            c.list_events()
        assert e.value.code == "bad_response"
