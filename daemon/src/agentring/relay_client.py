"""HTTP client for the relay REST API (protocol/relay.openapi.yaml)."""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__

ALLOW_INSECURE_ENV = "AGENTRING_ALLOW_INSECURE_RELAY"

_PRIVATE_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "100.64.0.0/10",  # CGNAT: Tailscale
    "::1/128", "fc00::/7", "fe80::/10"))
_SINGLE_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?$")
_NUMERIC_HOST = re.compile(r"^(0x[0-9a-f]+|[0-9]+)$")  # 134744072 is 8.8.8.8 to the resolver


def is_private_host(host: str) -> bool:
    """A host whose traffic stays on a network of the user's: loopback, private ranges, CGNAT,
    mDNS (.local), or a single-label name, which only resolves inside one (`relay` in Docker)."""
    h = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        if h == "localhost" or h.endswith((".localhost", ".local")):
            return True
        return bool(_SINGLE_LABEL.match(h)) and not _NUMERIC_HOST.match(h)
    ip = getattr(ip, "ipv4_mapped", None) or ip
    return ip.is_unspecified or any(ip in n for n in _PRIVATE_NETS)


class InsecureRelayError(ValueError):
    """A plain http:// URL on a host that is not private: credentials would cross the internet in clear."""


def check_relay_url(url: str) -> None:
    """Raises InsecureRelayError for http:// on a public host, unless the user opted in. Like the
    app's RelayUrlPolicy, plus single-label names, which the app cannot reach but a container can."""
    try:
        parts = urlsplit(url)
        scheme, host = parts.scheme.lower(), parts.hostname
    except ValueError:
        scheme, host = "http", None
    if scheme != "http" or os.environ.get(ALLOW_INSECURE_ENV) == "1":
        return
    if not host or not is_private_host(host):
        raise InsecureRelayError(f"refusing plain http:// to {host or url}: the API key would travel unencrypted. "
                                 f"Use https://, or set {ALLOW_INSECURE_ENV}=1 if that network is safe")


class RelayError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message} (HTTP {status})")
        self.status = status
        self.code = code


class RelayClient:
    def __init__(self, relay_url: str, api_key: str, *, timeout: float = 15.0, transport: httpx.BaseTransport | None = None):
        check_relay_url(relay_url)
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
        if 300 <= r.status_code < 400:  # never followed: the Authorization header would go along
            where = r.headers.get("location", "elsewhere")[:200]
            raise RelayError(r.status_code, "redirect", f"{self.relay_url} redirects to {where}: use that URL")
        if r.status_code >= 400:
            try:
                err = r.json()["error"]
                code, message = str(err.get("code", "error")), str(err.get("message", r.text))
            except (ValueError, KeyError, TypeError, AttributeError):
                code, message = "error", r.text[:300]
            # The key no longer opens this relay: rotated or revoked (401), or, on a hosted relay,
            # the account was disabled (403 tenant_disabled) or deleted (404 "Tenant not found").
            # The MCP HTTP server checks this to stop accepting the key.
            if r.status_code == 401 or code == "tenant_disabled" or (r.status_code == 404 and message == "Tenant not found"):
                self.unauthorized = True
            raise RelayError(r.status_code, code, message)
        if r.status_code == 204 or not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            raise RelayError(r.status_code, "bad_response", f"{self.relay_url} did not answer with JSON") from None

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
