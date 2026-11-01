"""agentring cloud end to end: a real relay in hub mode, `agentring mcp-http` resolving
tenant keys through the hub's admin API, the daemon's RelayClient against a tenant URL."""

from __future__ import annotations

import httpx

from agentring.relay_client import RelayClient, RelayError

from .conftest import HUB_ADMIN_TOKEN
from .test_e2e import simulate_phone_pairing
from .test_mcp_http import Client, start  # noqa: F401 (fixture)

ADMIN = {"authorization": f"Bearer {HUB_ADMIN_TOKEN}"}


def new_tenant(hub: str, **limits) -> dict:
    r = httpx.post(f"{hub}/admin/v1/tenants", headers=ADMIN, json={"agent_name": "Cloud", "limits": limits})
    assert r.status_code == 201, r.text
    return r.json()


def test_mcp_http_against_a_hub(hub: str, start):
    a, b = new_tenant(hub), new_tenant(hub, messages_per_day=1)
    base_a = a["tenant"]["base_url"]
    assert base_a == f"{hub}/t/{a['tenant']['tenant_id']}"

    # The daemon's client works unchanged against a tenant base URL.
    with RelayClient(base_a, a["api_key"]) as rc:
        assert rc.status()["agent_name"] == "Cloud"
        phone = simulate_phone_pairing(rc.create_pairing_token()["qr_payload"])
    with RelayClient(base_a, b["api_key"]) as wrong:
        try:
            wrong.status()
            raise AssertionError("another tenant's key must not work")
        except RelayError as e:
            assert e.status == 401

    s = start(admin_url=hub, admin_token=HUB_ADMIN_TOKEN, poll_s=0.3)
    ca = Client(s.url, a["api_key"])
    ca.initialize()
    st = ca.tool("status")["structuredContent"]
    assert [p["name"] for p in st["phones"]] == ["Test Phone"]

    sent = ca.tool("send_message", text="hello from the cloud")["structuredContent"]
    httpx.post(f"{base_a}/v1/messages", headers=phone["headers"], json={"text": "got it", "reply_to": sent["msg_id"]})
    replies = ca.tool("get_replies", wait_s=10)["structuredContent"]["replies"]
    assert [m["text"] for m in replies] == ["got it"]

    # Tenant B: its own (empty) phone list, and its plan's message limit shows as a tool error.
    cb = Client(s.url, b["api_key"])
    cb.initialize()
    assert cb.tool("status")["structuredContent"]["phones"] == []
    assert not cb.tool("send_message", text="one")["isError"]
    over = cb.tool("send_message", text="two")
    assert over["isError"] and "quota_exceeded" in over["content"][0]["text"]

    # Unknown and disabled accounts get 401 from the MCP server.
    assert Client(s.url, "ak_unknown").post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    tid_b = b["tenant"]["tenant_id"]
    assert httpx.patch(f"{hub}/admin/v1/tenants/{tid_b}", headers=ADMIN, json={"disabled": True}).status_code == 200
    s2 = start(admin_url=hub, admin_token=HUB_ADMIN_TOKEN)  # fresh key cache
    assert Client(s2.url, b["api_key"]).post({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401


def test_rotated_key_never_borrows_the_new_one(hub: str, start):
    a = new_tenant(hub)
    tid, old = a["tenant"]["tenant_id"], a["api_key"]
    s = start(admin_url=hub, admin_token=HUB_ADMIN_TOKEN, poll_s=0.3)
    c_old = Client(s.url, old)
    c_old.initialize()
    assert not c_old.tool("status")["isError"]  # the old key is now cached as valid

    new = httpx.post(f"{hub}/admin/v1/tenants/{tid}/api-key", headers=ADMIN).json()["api_key"]
    c_new = Client(s.url, new)
    c_new.initialize()
    assert not c_new.tool("send_message", text="with the new key")["isError"]
    # The old key must not ride on the new key's client: the relay refuses it...
    refused = c_old.tool("send_message", text="with the old key")
    assert refused["isError"] and "401" in refused["content"][0]["text"]
    # ...and from then on the MCP server refuses it too, while the new key keeps working.
    assert c_old.post({"jsonrpc": "2.0", "id": 99, "method": "ping"}).status_code == 401
    assert not c_new.tool("status")["isError"]
    msgs = httpx.get(f"{a['tenant']['base_url']}/v1/messages", headers={"authorization": f"Bearer {new}"}).json()
    assert [m["text"] for m in msgs["messages"]] == ["with the new key"]


def test_a_deleted_account_is_refused_at_once(hub: str, start):
    a = new_tenant(hub)
    s = start(admin_url=hub, admin_token=HUB_ADMIN_TOKEN)
    c = Client(s.url, a["api_key"])
    c.initialize()
    assert not c.tool("status")["isError"]  # cached as valid
    assert httpx.delete(f"{hub}/admin/v1/tenants/{a['tenant']['tenant_id']}", headers=ADMIN).status_code == 204
    assert c.tool("status")["isError"]  # the relay says the tenant is gone...
    assert c.post({"jsonrpc": "2.0", "id": 99, "method": "ping"}).status_code == 401  # ...so the key is dropped
