"""HTTP client for the relay REST API (protocol/relay.openapi.yaml)."""

from __future__ import annotations

from typing import Any

import httpx

from . import __version__


class RelayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message} (HTTP {status})")
        self.status = status
        self.code = code


class RelayClient:
    def __init__(self, relay_url: str, api_key: str, *, timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        self.relay_url = relay_url.rstrip("/")
        self.unauthorized = False
        self._http = httpx.Client(
            base_url=self.relay_url,
            headers={"authorization": f"Bearer {api_key}", "user-agent": f"agentring-daemon/{__version__}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> RelayClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as e:
            raise RelayError(0, "unreachable", f"cannot reach relay at {self.relay_url}: {e}") from e
        if r.status_code == 401:
            self.unauthorized = True  # the key was revoked or rotated (the MCP HTTP server checks this)
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                raise RelayError(r.status_code, err.get("code", "error"), err.get("message", r.text))
            except (ValueError, KeyError, TypeError):
                raise RelayError(r.status_code, "error", r.text[:300]) from None
        if r.status_code == 204 or not r.content:
            return None
        return r.json()

    # system
    def status(self) -> dict:
        return self._request("GET", "/v1/status")

    # pairing / devices
    def create_pairing_token(self, ttl_s: int | None = None) -> dict:
        return self._request("POST", "/v1/pairing-tokens", json={"ttl_s": ttl_s} if ttl_s else {})

    def list_devices(self, include_revoked: bool = False) -> list[dict]:
        params = {"include_revoked": "true"} if include_revoked else None
        return self._request("GET", "/v1/devices", params=params)["devices"]

    def revoke_device(self, device_id: str) -> None:
        self._request("DELETE", f"/v1/devices/{device_id}")

    # messages
    def post_message(self, text: str, *, reply_to: str | None = None, client_msg_id: str | None = None) -> dict:
        body: dict[str, Any] = {"text": text}
        if reply_to:
            body["reply_to"] = reply_to
        if client_msg_id:
            body["client_msg_id"] = client_msg_id
        return self._request("POST", "/v1/messages", json=body)

    def get_message(self, msg_id: str) -> dict:
        return self._request("GET", f"/v1/messages/{msg_id}")

    def mark_read(self, msg_id: str) -> None:
        self._request("POST", f"/v1/messages/{msg_id}/read")

    # calls
    def create_call(
        self,
        reason: str,
        *,
        priority: str = "normal",
        agent_name: str | None = None,
        device_ids: list[str] | None = None,
    ) -> dict:
        """Ring every paired phone, or only `device_ids` (a targeted call)."""
        body: dict[str, Any] = {"reason": reason, "priority": priority}
        if agent_name:
            body["agent_name"] = agent_name
        if device_ids is not None:
            body["device_ids"] = list(device_ids)
        return self._request("POST", "/v1/calls", json=body)

    def get_call(self, call_id: str) -> dict:
        return self._request("GET", f"/v1/calls/{call_id}")

    def accept_call(self, call_id: str) -> dict:
        return self._request("POST", f"/v1/calls/{call_id}/accept")

    def end_call(self, call_id: str) -> dict:
        return self._request("POST", f"/v1/calls/{call_id}/end")

    # events
    def list_events(self, after_seq: int = 0, limit: int = 500) -> dict:
        return self._request("GET", "/v1/events", params={"after_seq": after_seq, "limit": limit})

    def ws_url(self, after_seq: int) -> str:
        base = self.relay_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/v1/ws?after_seq={after_seq}"
