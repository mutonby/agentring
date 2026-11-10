"""`agentring mcp` as an agent sees it: a real subprocess over stdio against a real relay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import httpx
from typer.testing import CliRunner

from agentring.cli import app

from .conftest import API_KEY
from .test_e2e import simulate_phone_pairing


class Client:
    def __init__(self, env: dict[str, str]):
        self.p = subprocess.Popen([sys.executable, "-m", "agentring.cli", "mcp"], env=env, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.next_id = 0

    def send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        line = self.p.stdout.readline()
        assert line, self.p.stderr.read()
        msg = json.loads(line)
        assert msg["id"] == self.next_id
        return msg

    def tool(self, name: str, **args) -> dict:
        return self.request("tools/call", {"name": name, "arguments": args})["result"]

    def close(self) -> None:
        self.p.stdin.close()
        assert self.p.wait(10) == 0


def test_mcp_server_messages(relay: str, agentring_home):
    r = CliRunner().invoke(app, ["init", "--relay", relay, "--api-key", API_KEY])
    assert r.exit_code == 0, r.output
    tok = json.loads(CliRunner().invoke(app, ["pair", "--no-wait", "--json"]).output)
    phone = simulate_phone_pairing(tok["qr_payload"])

    env = {**os.environ, "AGENTRING_HOME": str(agentring_home), "GEMINI_API_KEY": ""}
    c = Client(env)
    try:
        init = c.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "test", "version": "1"}})["result"]
        assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "agentring"
        c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert c.request("ping")["result"] == {}
        names = [t["name"] for t in c.request("tools/list")["result"]["tools"]]
        assert names == ["send_message", "get_replies", "call", "status"]

        st = c.tool("status")
        assert not st["isError"] and st["structuredContent"]["phones"][0]["name"] == "Test Phone"

        sent = c.tool("send_message", text="¿Despliego?")
        msg_id = sent["structuredContent"]["msg_id"]
        assert json.loads(sent["content"][0]["text"]) == {"msg_id": msg_id}

        assert c.tool("get_replies")["structuredContent"] == {"replies": []}

        def phone_replies_later():
            time.sleep(1)
            httpx.post(f"{relay}/v1/messages", headers=phone["headers"], json={"text": "sí", "reply_to": msg_id})

        threading.Thread(target=phone_replies_later).start()
        t0 = time.time()
        got = c.tool("get_replies", wait_s=20)["structuredContent"]["replies"]
        assert [(m["text"], m["reply_to"]) for m in got] == [("sí", msg_id)]
        assert time.time() - t0 < 15
        assert c.tool("get_replies")["structuredContent"] == {"replies": []}  # each reply once

        call = c.tool("call", reason="test")
        assert call["isError"] and ("GEMINI_API_KEY" in call["content"][0]["text"]
                                    or "[voice]" in call["content"][0]["text"])
        assert c.tool("send_message")["isError"]  # missing text
        assert c.request("tools/call", {"name": "nope"})["error"]["code"] == -32602
        assert c.request("nope/method")["error"]["code"] == -32601
    finally:
        c.close()


def test_server_starts_without_configuration() -> None:
    """Not configured yet: the server still lists its tools (clients and directories inspect it
    first) and a tool call says what is missing instead of the process exiting."""
    import asyncio

    from agentring.mcp_server import NotConfiguredTools, handle_rpc

    tools = NotConfiguredTools("Run: agentring init")

    async def run() -> tuple[dict, dict]:
        listed = await handle_rpc(tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})  # type: ignore[arg-type]
        called = await handle_rpc(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",  # type: ignore[arg-type]
                                          "params": {"name": "send_message", "arguments": {"text": "hi"}}})
        assert listed and called
        return listed, called

    listed, called = asyncio.run(run())
    assert {t["name"] for t in listed["result"]["tools"]} == {"send_message", "get_replies", "call", "status"}
    assert called["result"]["isError"] is True
    assert "not set up" in called["result"]["content"][0]["text"] and "agentring init" in called["result"]["content"][0]["text"]
