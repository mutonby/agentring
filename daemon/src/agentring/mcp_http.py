"""agentring as a remote MCP server (Streamable HTTP), for many users at once.

    agentring mcp-http --relay-url https://relay.example.com      # self-hosted: one relay
    AGENTRING_HUB_ADMIN_URL=http://relay:8080 AGENTRING_HUB_ADMIN_TOKEN_FILE=... agentring mcp-http

    claude mcp add --transport http agentring https://host/mcp --header "Authorization: Bearer ak_..."

Same tools as `agentring mcp` (mcp_server.py: `AgentringTools`, `handle_rpc`), served at
`POST /mcp` per the MCP Streamable HTTP transport. Each request carries the user's relay API
key (`Authorization: Bearer ak_...`); the key picks the relay (hub tenant, or the one relay in
self-hosted mode). Quick requests get `application/json`; tool calls that wait (`call`,
`get_replies` with `wait_s`) get a `text/event-stream` with keep-alive comments until the
result. A small HTTP/1.1 server on asyncio streams: no web framework, no MCP SDK.

Never logged: API keys, the admin token, request bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__
from .config import ConfigError, home_dir
from .mcp_server import (PROTOCOL_VERSIONS, AgentringTools, CallSlots, bad_request_id, cancelled_request_id,
                         handle_rpc, initialize_result, is_request, rpc_response)
from .relay_client import RelayClient
from .store import Store

log = logging.getLogger("agentring.mcp_http")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
DEFAULT_MAX_CALLS = 4
DEFAULT_MAX_BODY = 1024 * 1024
MAX_HEADER_BYTES = 16 * 1024
KEEPALIVE_S = 15.0
RESOLVE_TTL_S = 300.0
SESSION_IDLE_S = 24 * 3600.0
MAX_SESSIONS = 10_000
MAX_OPEN_TENANTS = 256
IDLE_CONNECTION_S = 75.0
READ_TIMEOUT_S = 30.0

_UNPARSABLE = object()  # a request body that is not JSON
# A relay API key, or (hub mode) a short-lived agent token; ≤ 128 chars, like the hub's resolve-key.
_API_KEY = re.compile(r"^a[kt]_[\x21-\x7e]{1,125}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
REASONS = {200: "OK", 202: "Accepted", 204: "No Content", 400: "Bad Request", 401: "Unauthorized",
           403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 408: "Request Timeout",
           411: "Length Required", 413: "Content Too Large", 415: "Unsupported Media Type",
           431: "Request Header Fields Too Large", 500: "Internal Server Error", 501: "Not Implemented",
           503: "Service Unavailable"}


def key_hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


# ------------------------------------------------------------------ configuration


@dataclass
class HttpConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    relay_url: str | None = None  # self-hosted: every key goes to this relay; hub mode: the relay's internal address
    admin_url: str | None = None  # hub mode: resolve keys with POST {admin_url}/admin/v1/resolve-key
    admin_token: str | None = None
    data_dir: Path = field(default_factory=lambda: home_dir() / "mcp-http")
    max_calls: int = DEFAULT_MAX_CALLS
    max_body: int = DEFAULT_MAX_BODY
    allowed_origins: tuple[str, ...] = ()  # browser Origins allowed to call /mcp ("*" = any)
    keepalive_s: float = KEEPALIVE_S
    resolve_ttl_s: float = RESOLVE_TTL_S
    poll_s: float = 2.0  # get_replies polling

    @property
    def hub(self) -> bool:
        return self.admin_url is not None

    @classmethod
    def from_env(cls, **cli: Any) -> HttpConfig:
        """CLI values (None = not given) win over the environment, which wins over the defaults."""
        env = os.environ.get

        def pick(name: str, var: str, default: Any, conv: Any = str) -> Any:
            if cli.get(name) is not None:
                return cli[name]
            raw = env(var)
            if raw in (None, ""):
                return default
            try:
                return conv(raw)
            except ValueError:
                raise ConfigError(f"{var}: invalid value {raw!r}") from None

        admin_url = pick("admin_url", "AGENTRING_HUB_ADMIN_URL", None)
        token = None
        token_file = env("AGENTRING_HUB_ADMIN_TOKEN_FILE")
        if token_file:
            try:
                token = Path(token_file).expanduser().read_text().strip()
            except OSError as e:
                raise ConfigError(f"AGENTRING_HUB_ADMIN_TOKEN_FILE: {e}") from None
        token = token or env("AGENTRING_HUB_ADMIN_TOKEN") or None
        relay_url = pick("relay_url", "AGENTRING_RELAY_URL", None)
        if admin_url and not token:
            raise ConfigError("AGENTRING_HUB_ADMIN_URL needs the admin token: AGENTRING_HUB_ADMIN_TOKEN_FILE "
                              "or AGENTRING_HUB_ADMIN_TOKEN")
        if not admin_url and not relay_url:
            raise ConfigError("set the relay (--relay-url / AGENTRING_RELAY_URL), or a hub "
                              "(AGENTRING_HUB_ADMIN_URL and its admin token)")
        origins = cli.get("allowed_origins")
        if origins is None:
            origins = [o for o in (env("AGENTRING_MCP_ALLOWED_ORIGINS") or "").split(",")]
        data_dir = pick("data_dir", "AGENTRING_MCP_DATA_DIR", None, Path)
        cfg = cls(
            host=pick("host", "AGENTRING_MCP_HOST", DEFAULT_HOST),
            port=pick("port", "AGENTRING_MCP_PORT", DEFAULT_PORT, int),
            relay_url=relay_url.rstrip("/") if relay_url else None,
            admin_url=admin_url.rstrip("/") if admin_url else None,
            admin_token=token if admin_url else None,
            max_calls=pick("max_calls", "AGENTRING_MCP_MAX_CALLS", DEFAULT_MAX_CALLS, int),
            max_body=pick("max_body", "AGENTRING_MCP_MAX_BODY", DEFAULT_MAX_BODY, int),
            allowed_origins=tuple(o.strip().rstrip("/") for o in origins if o.strip()),
        )
        if data_dir is not None:
            cfg.data_dir = Path(data_dir).expanduser()
        return cfg


# ------------------------------------------------------------------ key → relay


@dataclass(frozen=True)
class Tenant:
    tenant_id: str  # hub tenant id, or relay_<relay_id> in self-hosted mode; names its state file
    base_url: str  # the relay API for this key (a hub tenant's is .../t/{tenant_id})


class Unauthorized(Exception):
    pass


class UpstreamError(Exception):
    """The hub admin API or the relay can't answer right now."""


def _error_code(r: httpx.Response) -> str | None:
    """The relay's `{"error": {"code"}}`, if the response carries one."""
    try:
        return str(r.json()["error"]["code"])
    except (ValueError, KeyError, TypeError):
        return None


class KeyResolver:
    """API key → Tenant. Hub mode asks the admin API; self-hosted mode checks the key against
    the one relay (`GET /v1/status`). Positive answers are cached for `ttl_s`, keyed by a hash
    of the key; failures are not."""

    def __init__(self, cfg: HttpConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._http = httpx.AsyncClient(timeout=10.0, transport=transport,
                                       headers={"user-agent": f"agentring-mcp-http/{__version__}"})
        self._cache: dict[str, tuple[Tenant, float]] = {}
        self._inflight: dict[str, asyncio.Future[Tenant]] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    def forget(self, api_key: str) -> None:
        self._cache.pop(key_hash(api_key), None)

    async def resolve(self, api_key: str) -> Tenant:
        h = key_hash(api_key)
        hit = self._cache.get(h)
        if hit and hit[1] > time.monotonic():
            return hit[0]
        if h in self._inflight:  # one lookup per key at a time
            return await asyncio.shield(self._inflight[h])
        fut: asyncio.Future[Tenant] = asyncio.get_running_loop().create_future()
        self._inflight[h] = fut
        try:
            tenant = await (self._hub(api_key) if self.cfg.hub else self._self_hosted(api_key))
        except BaseException as e:
            fut.set_exception(e if isinstance(e, Exception) else UpstreamError("cancelled"))
            fut.exception()  # retrieved: no "never retrieved" warning when nobody else waited
            raise
        else:
            fut.set_result(tenant)
            if len(self._cache) >= MAX_SESSIONS:
                now = time.monotonic()
                for k in [k for k, (_, exp) in self._cache.items() if exp <= now] or list(self._cache)[:1000]:
                    self._cache.pop(k, None)
            self._cache[h] = (tenant, time.monotonic() + self.cfg.resolve_ttl_s)
            return tenant
        finally:
            self._inflight.pop(h, None)

    async def _hub(self, api_key: str) -> Tenant:
        try:
            r = await self._http.post(f"{self.cfg.admin_url}/admin/v1/resolve-key", json={"api_key": api_key},
                                      headers={"authorization": f"Bearer {self.cfg.admin_token}"})
        except httpx.HTTPError as e:
            log.warning("hub admin API unreachable: %s", type(e).__name__)
            raise UpstreamError("hub unreachable") from None
        if r.status_code == 404 or _error_code(r) == "tenant_disabled":
            raise Unauthorized()
        if r.status_code in (401, 403):
            log.error("hub admin API rejected the admin token (HTTP %d)", r.status_code)
            raise UpstreamError("hub refused us")
        if r.status_code != 200:
            log.warning("hub admin API answered HTTP %d to resolve-key", r.status_code)
            raise UpstreamError(f"hub HTTP {r.status_code}")
        try:
            d = r.json()
            tid, base = str(d["tenant_id"]), str(d["base_url"]).rstrip("/")
        except (ValueError, KeyError, TypeError):
            raise UpstreamError("bad resolve-key answer") from None
        if not _SAFE_ID.match(tid) or urlsplit(base).scheme not in ("http", "https"):
            raise UpstreamError("bad resolve-key answer")
        if self.cfg.relay_url:  # reach the relay on its internal address, not back through the proxy
            base = f"{self.cfg.relay_url}/t/{tid}"
        return Tenant(tid, base)

    async def _self_hosted(self, api_key: str) -> Tenant:
        base = self.cfg.relay_url or ""
        try:
            r = await self._http.get(f"{base}/v1/status", headers={"authorization": f"Bearer {api_key}"})
        except httpx.HTTPError as e:
            log.warning("relay unreachable: %s", type(e).__name__)
            raise UpstreamError("relay unreachable") from None
        if r.status_code in (401, 403):
            raise Unauthorized()
        if r.status_code != 200:
            raise UpstreamError(f"relay HTTP {r.status_code}")
        try:
            relay_id = str(r.json()["relay_id"])
        except (ValueError, KeyError, TypeError):
            raise UpstreamError("bad relay status") from None
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", relay_id)[:64] or "relay"
        return Tenant(f"relay_{safe}", base)


# ------------------------------------------------------------------ per-tenant tools


@dataclass
class _Open:
    tools: AgentringTools  # the tenant's store and locks; each request gets a view with its own key's client
    relay_url: str
    clients: dict[str, RelayClient] = field(default_factory=dict)  # by hash of the API key
    active: int = 0
    last_used: float = 0.0
    retired: list[RelayClient] = field(default_factory=list)  # clients of dropped keys, closed when idle


class TenantPool:
    """One store per tenant (shared by all its sessions, so a reply is returned once): a SQLite
    file under `data_dir/tenants/` that mirrors the tenant's messages and remembers which replies
    get_replies returned. Each API key gets its own relay client, so a request never runs with
    another request's credentials (e.g. the old and new key during a rotation). Idle tenants are
    closed when more than `max_open` are open; their state stays on disk."""

    def __init__(self, data_dir: Path, *, call_slots: CallSlots, poll_s: float = 2.0, max_open: int = MAX_OPEN_TENANTS):
        self.dir = data_dir / "tenants"
        self.dir.mkdir(parents=True, exist_ok=True)
        for d in (data_dir, self.dir):
            try:
                d.chmod(0o700)
            except OSError:
                pass
        self.call_slots = call_slots
        self.poll_s = poll_s
        self.max_open = max_open
        self._open: dict[str, _Open] = {}

    def acquire(self, tenant: Tenant, api_key: str) -> AgentringTools:
        e = self._open.get(tenant.tenant_id)
        if e is not None and e.relay_url != tenant.base_url:  # the tenant moved: start its clients over
            e.retired.extend(e.clients.values())
            e.clients.clear()
            e.relay_url = tenant.base_url
        if e is None:
            self._evict()
            client = RelayClient(tenant.base_url, api_key)
            tools = AgentringTools(client, Store(self.dir / f"{tenant.tenant_id}.db"),
                                   relay_url=tenant.base_url, poll_s=self.poll_s, agents_file=None,
                                   call_slots=self.call_slots)
            e = self._open[tenant.tenant_id] = _Open(tools, tenant.base_url, {key_hash(api_key): client})
        h = key_hash(api_key)
        client = e.clients.get(h)
        if client is None:
            client = e.clients[h] = RelayClient(tenant.base_url, api_key)
        e.active += 1
        e.last_used = time.monotonic()
        return e.tools.with_client(client, tenant.base_url)

    def release(self, tenant_id: str, api_key: str | None = None) -> bool:
        """Done with a request. Returns True when the relay refused that request's key (it was
        rotated or revoked): its client is dropped, and the caller should forget the key."""
        e = self._open.get(tenant_id)
        if e is None:
            return False
        refused = False
        if api_key is not None:
            h = key_hash(api_key)
            c = e.clients.get(h)
            if c is not None and c.unauthorized:
                e.retired.append(e.clients.pop(h))
                refused = True
        e.active = max(0, e.active - 1)
        e.last_used = time.monotonic()
        if e.active == 0 and not e.tools.busy:
            for c in e.retired:
                c.close()
            e.retired.clear()
        return refused

    def _evict(self) -> None:
        if len(self._open) < self.max_open:
            return
        idle = sorted((e.last_used, tid) for tid, e in self._open.items() if e.active == 0 and not e.tools.busy)
        for _, tid in idle[: max(1, len(self._open) - self.max_open + 1)]:
            self._close(self._open.pop(tid))

    def _close(self, e: _Open) -> None:
        for c in [*e.retired, *e.clients.values()]:
            c.close()
        e.tools.store.close()

    def close_all(self) -> None:
        for e in self._open.values():
            self._close(e)
        self._open.clear()


# ------------------------------------------------------------------ sessions


@dataclass
class Session:
    sid: str
    tenant_id: str
    protocol_version: str
    last_seen: float
    running: dict[Any, asyncio.Task] = field(default_factory=dict)


# ------------------------------------------------------------------ HTTP


class HttpError(Exception):
    def __init__(self, status: int, message: str = "", headers: dict[str, str] | None = None, rpc: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message or REASONS.get(status, "error")
        self.headers = headers or {}
        self.rpc = rpc  # body as a JSON-RPC error


@dataclass
class Request:
    method: str
    path: str
    version: str
    headers: dict[str, str]
    body: bytes = b""

    @property
    def keep_alive(self) -> bool:
        conn = self.headers.get("connection", "").lower()
        return "close" not in conn if self.version == "HTTP/1.1" else "keep-alive" in conn


def _unauthorized(error: str | None = None) -> HttpError:
    challenge = 'Bearer realm="agentring"' + (f', error="{error}"' if error else "")
    return HttpError(401, "a relay API key is needed: Authorization: Bearer ak_..." if not error
                     else "unknown API key", {"www-authenticate": challenge}, rpc=True)


def _slow(m: Any) -> bool:
    """Requests that may wait a long time: answered over SSE with keep-alives."""
    if not is_request(m) or m["method"] != "tools/call":
        return False
    p = m.get("params") if isinstance(m.get("params"), dict) else {}
    if p.get("name") == "call":
        return True
    if p.get("name") == "get_replies":
        args = p.get("arguments") if isinstance(p.get("arguments"), dict) else {}
        try:
            return int(args.get("wait_s") or 0) > 0
        except (TypeError, ValueError):
            return False
    return False


def _cancel_on_disconnect(m: Any) -> bool:
    """When the client goes away, a waiting get_replies stops (or it would mark replies as
    returned that nobody got); a phone call goes on, the user is on it."""
    p = m.get("params") if isinstance(m.get("params"), dict) else {}
    return p.get("name") != "call"


class McpHttpServer:
    def __init__(self, cfg: HttpConfig, *, resolver: KeyResolver | None = None):
        self.cfg = cfg
        self.resolver = resolver or KeyResolver(cfg)
        self.call_slots = CallSlots(cfg.max_calls)
        self.pool = TenantPool(cfg.data_dir, call_slots=self.call_slots, poll_s=cfg.poll_s)
        self.sessions: dict[str, Session] = {}
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[asyncio.Task] = set()
        self._background: set[asyncio.Task] = set()

    # lifecycle
    async def start(self) -> tuple[str, int]:
        self._server = await asyncio.start_server(self._connection, self.cfg.host, self.cfg.port,
                                                  limit=MAX_HEADER_BYTES)
        host, port = self._server.sockets[0].getsockname()[:2]
        mode = f"hub {self.cfg.admin_url}" if self.cfg.hub else f"relay {self.cfg.relay_url}"
        log.info("agentring %s MCP over HTTP on http://%s:%d/mcp (%s, max %d calls)",
                 __version__, host, port, mode, self.cfg.max_calls)
        return host, port

    async def aclose(self) -> None:
        if self._server is not None:
            self._server.close()
        for s in list(self.sessions.values()):
            for t in s.running.values():
                t.cancel()
        tasks = [*self._background, *self._conns]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
        await self.resolver.aclose()
        self.pool.close_all()

    # connections
    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._conns.add(task)
        try:
            first = True
            while True:
                try:
                    req = await self._read_request(reader, IDLE_CONNECTION_S if not first else READ_TIMEOUT_S)
                except HttpError as e:
                    await self._send(writer, e.status, self._error_body(e), e.headers, keep_alive=False)
                    return
                if req is None:
                    return
                first = False
                keep = await self._handle(req, reader, writer)
                if not keep:
                    return
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        except Exception:
            log.exception("connection failed")
        finally:
            if task is not None:
                self._conns.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _read_request(self, reader: asyncio.StreamReader, idle_s: float) -> Request | None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), idle_s)
        except asyncio.IncompleteReadError as e:
            if e.partial.strip():
                raise HttpError(400) from None
            return None  # the client closed the connection
        except asyncio.LimitOverrunError:
            raise HttpError(431) from None
        except asyncio.TimeoutError:
            return None
        try:
            lines = head.decode("latin-1").split("\r\n")
            method, target, version = lines[0].split(" ")
        except ValueError:
            raise HttpError(400) from None
        if version not in ("HTTP/1.1", "HTTP/1.0"):
            raise HttpError(400)
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, sep, value = line.partition(":")
            if not sep or not name or name != name.strip():
                raise HttpError(400)
            k = name.lower()
            headers[k] = f"{headers[k]}, {value.strip()}" if k in headers else value.strip()
        req = Request(method.upper(), target.split("?", 1)[0], version, headers)
        te = headers.get("transfer-encoding", "").lower()
        if te:
            if te != "chunked" or "content-length" in headers:
                raise HttpError(400)
            req.body = await asyncio.wait_for(self._read_chunked(reader), READ_TIMEOUT_S)
        elif "content-length" in headers:
            try:
                n = int(headers["content-length"])
            except ValueError:
                raise HttpError(400) from None
            if n < 0:
                raise HttpError(400)
            if n > self.cfg.max_body:
                raise HttpError(413, f"request body over {self.cfg.max_body} bytes")
            req.body = await asyncio.wait_for(reader.readexactly(n), READ_TIMEOUT_S)
        elif req.method == "POST":
            raise HttpError(411)
        return req

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        body = bytearray()
        while True:
            line = await reader.readuntil(b"\r\n")
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise HttpError(400) from None
            if size == 0:
                while (await reader.readuntil(b"\r\n")) != b"\r\n":
                    pass  # trailers
                return bytes(body)
            if len(body) + size > self.cfg.max_body:
                raise HttpError(413, f"request body over {self.cfg.max_body} bytes")
            body += await reader.readexactly(size)
            if await reader.readexactly(2) != b"\r\n":
                raise HttpError(400)

    @staticmethod
    def _error_body(e: HttpError) -> dict[str, Any]:
        if e.rpc:
            code = -32001 if e.status == 401 else -32603 if e.status >= 500 else -32600
            return rpc_response(None, error=(code, e.message))
        return {"error": {"code": REASONS.get(e.status, "error").lower().replace(" ", "_"), "message": e.message}}

    async def _send(self, writer: asyncio.StreamWriter, status: int, body: Any = None,
                    headers: dict[str, str] | None = None, *, keep_alive: bool = True) -> None:
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        h = {"content-length": str(len(data)), **(headers or {})}
        if data:
            h.setdefault("content-type", "application/json")
        if not keep_alive:
            h["connection"] = "close"
        writer.write(self._head(status, h) + data)
        await writer.drain()

    @staticmethod
    def _head(status: int, headers: dict[str, str]) -> bytes:
        lines = [f"HTTP/1.1 {status} {REASONS.get(status, 'Unknown')}", "server: agentring",
                 *(f"{k}: {v}" for k, v in headers.items())]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")

    # routing
    async def _handle(self, req: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
        """Answers one request; False closes the connection."""
        t0 = time.monotonic()
        info: dict[str, Any] = {}
        status = 500
        try:
            if req.path == "/healthz" and req.method == "GET":
                status = 200
                await self._send(writer, 200, {"ok": True}, keep_alive=req.keep_alive)
                return req.keep_alive
            if req.path != "/mcp":
                raise HttpError(404)
            self._check_origin(req)
            if req.method == "POST":
                status, keep = await self._post(req, reader, writer, info)
                return keep and req.keep_alive
            if req.method == "DELETE":
                status = await self._delete(req, writer, info)
                return req.keep_alive
            raise HttpError(405, "use POST (this server opens no stream on GET)", {"allow": "POST, DELETE"})
        except HttpError as e:
            status = e.status
            await self._send(writer, e.status, self._error_body(e), e.headers, keep_alive=req.keep_alive)
            return req.keep_alive
        finally:
            if req.path != "/healthz":
                extra = " ".join(f"{k}={v}" for k, v in info.items())
                log.info("%s %s %d %.0fms %s", req.method, req.path, status, (time.monotonic() - t0) * 1000, extra)

    def _check_origin(self, req: Request) -> None:
        """Browsers send Origin: only listed ones may use the server (DNS rebinding, CSRF)."""
        origin = req.headers.get("origin")
        if origin and "*" not in self.cfg.allowed_origins and origin.rstrip("/") not in self.cfg.allowed_origins:
            raise HttpError(403, "origin not allowed")

    async def _auth(self, req: Request, info: dict[str, Any], *, fresh: bool = False) -> tuple[str, Tenant]:
        """The request's key and tenant. `fresh` skips the resolver cache (a new session checks
        that the key still works, e.g. right after the account was deleted)."""
        scheme, _, key = req.headers.get("authorization", "").partition(" ")
        key = key.strip()
        if scheme.lower() != "bearer" or not key:
            raise _unauthorized()
        if not _API_KEY.match(key):
            raise _unauthorized("invalid_token")
        if fresh:
            self.resolver.forget(key)
        try:
            tenant = await self.resolver.resolve(key)
        except Unauthorized:
            raise _unauthorized("invalid_token") from None
        except UpstreamError:
            raise HttpError(503, "cannot check the API key right now; try again", {"retry-after": "5"},
                            rpc=True) from None
        info["tenant"] = tenant.tenant_id
        return key, tenant

    def _session(self, req: Request, tenant: Tenant) -> Session | None:
        sid = req.headers.get("mcp-session-id")
        if sid is None:
            return None  # stateless use is fine: sessions only add cancellation and DELETE
        s = self.sessions.get(sid)
        now = time.monotonic()
        if s is not None and not s.running and now - s.last_seen > SESSION_IDLE_S:
            self.sessions.pop(sid, None)
            s = None
        if s is None or s.tenant_id != tenant.tenant_id:
            raise HttpError(404, "unknown or expired session; initialize again", rpc=True)
        s.last_seen = now
        return s

    def _new_session(self, tenant: Tenant, version: str) -> Session:
        now = time.monotonic()
        if len(self.sessions) >= MAX_SESSIONS:
            idle = sorted((s.last_seen, sid) for sid, s in self.sessions.items() if not s.running)
            stale = [sid for seen, sid in idle if now - seen > SESSION_IDLE_S] or [sid for _, sid in idle[:100]]
            for sid in stale:
                self.sessions.pop(sid, None)
        s = Session(secrets.token_urlsafe(32), tenant.tenant_id, version, now)
        self.sessions[s.sid] = s
        return s

    async def _delete(self, req: Request, writer: asyncio.StreamWriter, info: dict[str, Any]) -> int:
        _, tenant = await self._auth(req, info)
        if "mcp-session-id" not in req.headers:
            raise HttpError(400, "Mcp-Session-Id header needed")
        s = self._session(req, tenant)
        assert s is not None
        self.sessions.pop(s.sid, None)
        for t in s.running.values():
            t.cancel()
        await self._send(writer, 204, keep_alive=req.keep_alive)
        return 204

    async def _post(self, req: Request, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                    info: dict[str, Any]) -> tuple[int, bool]:
        try:
            body: Any = json.loads(req.body)
        except ValueError:
            body = _UNPARSABLE
        # A new session re-checks the key without the cache (e.g. right after the account was deleted).
        key, tenant = await self._auth(req, info, fresh=isinstance(body, dict) and body.get("method") == "initialize")
        ctype = req.headers.get("content-type", "application/json").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HttpError(415, "send application/json", rpc=True)
        version = req.headers.get("mcp-protocol-version")
        if version is not None and version not in PROTOCOL_VERSIONS:
            raise HttpError(400, f"unsupported MCP-Protocol-Version {version}; supported: "
                            f"{', '.join(PROTOCOL_VERSIONS)}", rpc=True)
        if body is _UNPARSABLE:
            await self._send(writer, 400, rpc_response(None, error=(-32700, "parse error")), keep_alive=req.keep_alive)
            return 400, True
        batch = isinstance(body, list)
        msgs = body if batch else [body]
        if not msgs or not all(isinstance(m, dict) for m in msgs):
            raise HttpError(400, "invalid JSON-RPC message", rpc=True)

        if any(m.get("method") == "initialize" for m in msgs):
            if batch:
                raise HttpError(400, "initialize must be sent on its own", rpc=True)
            info["rpc"] = "initialize"
            if not is_request(body):
                raise HttpError(400, "initialize is a request: it needs an id", rpc=True)
            params = body.get("params") if isinstance(body.get("params"), dict) else {}
            result = initialize_result(params)
            s = self._new_session(tenant, result["protocolVersion"])
            await self._send(writer, 200, rpc_response(body["id"], result), {"mcp-session-id": s.sid},
                             keep_alive=req.keep_alive)
            return 200, True

        if any(bad_request_id(m) for m in msgs):
            raise HttpError(400, "invalid request: id must be a string or a number", rpc=True)
        session = self._session(req, tenant)
        requests = [m for m in msgs if is_request(m)]
        for m in msgs:
            rid = cancelled_request_id(m)
            if rid is not None and session is not None:
                t = session.running.get(rid)
                if t is not None:
                    t.cancel()
        if not requests:
            info["rpc"] = ",".join(str(m.get("method", "response")) for m in msgs)[:80]
            await self._send(writer, 202, keep_alive=req.keep_alive)
            return 202, True
        info["rpc"] = ",".join(_describe(m) for m in requests)[:120]

        tools = self.pool.acquire(tenant, key)
        tasks: list[tuple[dict[str, Any], asyncio.Task]] = []
        for m in requests:
            t = asyncio.create_task(handle_rpc(tools, m))
            tasks.append((m, t))
            if session is not None:
                session.running[m["id"]] = t
        done_all = asyncio.ensure_future(self._finish(tenant, key, session, tasks))
        self._background.add(done_all)
        done_all.add_done_callback(self._background.discard)

        stream = "text/event-stream" in req.headers.get("accept", "") and any(_slow(m) for m, _ in tasks)
        if not stream:
            await asyncio.wait([t for _, t in tasks])
            out = [r for r in (_outcome(m, t) for m, t in tasks) if r is not None]
            if not out:  # all cancelled
                await self._send(writer, 202, keep_alive=req.keep_alive)
                return 202, True
            await self._send(writer, 200, out if batch else out[0], keep_alive=req.keep_alive)
            return 200, True
        await self._stream(writer, reader, tasks)
        return 200, False

    async def _finish(self, tenant: Tenant, key: str, session: Session | None,
                      tasks: list[tuple[dict[str, Any], asyncio.Task]]) -> None:
        """Releases the tenant once every request of a POST is done (even after the client left)."""
        try:
            await asyncio.gather(*(t for _, t in tasks), return_exceptions=True)
        finally:
            if session is not None:
                for m, t in tasks:
                    if session.running.get(m["id"]) is t:
                        session.running.pop(m["id"], None)
            if self.pool.release(tenant.tenant_id, key):
                self.resolver.forget(key)  # rotated or revoked: the next request gets an HTTP 401

    async def _stream(self, writer: asyncio.StreamWriter, reader: asyncio.StreamReader,
                      tasks: list[tuple[dict[str, Any], asyncio.Task]]) -> None:
        """SSE: each response as an event when ready, a keep-alive comment every keepalive_s."""
        writer.write(self._head(200, {"content-type": "text/event-stream", "cache-control": "no-cache, no-transform",
                                      "x-accel-buffering": "no", "connection": "close"}))
        pending = {t: m for m, t in tasks}
        gone: asyncio.Future = asyncio.ensure_future(reader.read(1))  # b"" when the client hangs up
        try:
            await writer.drain()
            while pending:
                done, _ = await asyncio.wait({*pending, gone}, timeout=self.cfg.keepalive_s,
                                             return_when=asyncio.FIRST_COMPLETED)
                if gone in done:
                    if gone.exception() is not None or gone.result() == b"":
                        raise ConnectionResetError("client went away")
                    gone = asyncio.get_running_loop().create_future()  # stray bytes: stop watching
                chunks = []
                for t in done - {gone}:
                    m = pending.pop(t, None)
                    r = _outcome(m, t) if m is not None else None
                    if r is not None:
                        chunks.append(f"event: message\ndata: {json.dumps(r, ensure_ascii=False)}\n\n")
                writer.write(("".join(chunks) or ": keep-alive\n\n").encode())
                await writer.drain()
        except (ConnectionError, OSError):
            for t, m in pending.items():
                if _cancel_on_disconnect(m):
                    t.cancel()
            if pending:
                log.info("client left; %d request(s) %s", len(pending),
                         "go on" if any(not _cancel_on_disconnect(m) for m in pending.values()) else "cancelled")
        finally:
            gone.cancel()


def _outcome(m: dict[str, Any], t: asyncio.Task) -> dict[str, Any] | None:
    """A finished request's response; None if it was cancelled (no response is due)."""
    if t.cancelled():
        return None
    if t.exception() is not None:
        log.error("request %s failed", _describe(m), exc_info=t.exception())
        return rpc_response(m["id"], error=(-32603, "internal error"))
    return t.result()


def _describe(m: dict[str, Any]) -> str:
    if m.get("method") == "tools/call" and isinstance(m.get("params"), dict):
        return f"tools/call:{m['params'].get('name')}"
    return str(m.get("method"))


def run_http(cfg: HttpConfig) -> None:
    async def main() -> None:
        loop = asyncio.get_running_loop()
        # Relay requests run in threads (sync httpx); many tenants waiting at once need more than the default.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=64, thread_name_prefix="agentring-mcp"))
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        server = McpHttpServer(cfg)
        await server.start()
        try:
            await stop.wait()
        finally:
            log.info("stopping")
            await server.aclose()

    asyncio.run(main())
