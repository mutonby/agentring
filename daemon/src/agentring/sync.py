"""Keeps the local store in sync with the relay event log (catch-up over REST, live over WebSocket)."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from . import __version__
from .relay_client import RelayClient
from .store import Store

log = logging.getLogger("agentring.sync")

CLOSE_UNAUTHORIZED = 4401
CLOSE_REPLACED = 4409
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


class FatalSyncError(Exception):
    """Reconnecting will not help (bad API key, another daemon took over)."""


def _clean(s: Any) -> Any:
    """A lone surrogate (half an emoji from a client that cut a string) can't be stored as
    UTF-8: U+FFFD instead, which is what the relay's own messages table keeps."""
    return _LONE_SURROGATE.sub("\ufffd", s) if isinstance(s, str) else s


def apply_event(store: Store, event: dict[str, Any]) -> None:
    """Applies one relay event to the local store. Idempotent.

    An event we can't store is logged and skipped, and the cursor moves past it: the event
    log keeps it for good, so stopping here would stop sync on every run. sqlite3.Error is
    left to the caller on purpose: a database that is locked for a moment says nothing about
    the event, and moving the cursor then would lose a good message."""
    t = event.get("type")
    try:
        if t == "msg.new":
            store.upsert_message(event["msg_id"], event["from"], _clean(event["text"]), event["ts"],
                                 _clean(event.get("reply_to")))
        elif t == "msg.read":
            store.mark_read(event["msg_id"], event["ts"])
    except (ValueError, KeyError, TypeError) as e:
        log.warning("skipping event seq=%s (%s): %s", event.get("seq"), t, type(e).__name__)
    seq = event.get("seq")
    if isinstance(seq, int):
        store.advance_seq(seq)


def catch_up(client: RelayClient, store: Store) -> int:
    """Pulls every event after our cursor. Returns how many were applied."""
    store.reset_if_relay_changed(client.status()["relay_id"])
    n = 0
    while True:
        page = client.list_events(after_seq=store.last_seq)
        for ev in page["events"]:
            apply_event(store, ev)
            n += 1
        if not page["has_more"]:
            return n


async def run_ws(
    client: RelayClient,
    api_key: str,
    store: Store,
    on_event: Callable[[dict[str, Any]], None] = lambda e: None,
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Holds the WebSocket open forever, reconnecting with backoff and resuming from `last_seq`."""
    stop = stop or asyncio.Event()
    backoff = 1.0
    headers = {"authorization": f"Bearer {api_key}", "user-agent": f"agentring-daemon/{__version__}"}
    while not stop.is_set():
        url = client.ws_url(store.last_seq)
        try:
            async with websockets.connect(url, additional_headers=headers, ping_interval=20, ping_timeout=20) as ws:
                log.info("connected to relay")
                backoff = 1.0
                stopper = asyncio.ensure_future(stop.wait())
                try:
                    while True:
                        recv = asyncio.ensure_future(ws.recv())
                        done, _ = await asyncio.wait({recv, stopper}, return_when=asyncio.FIRST_COMPLETED)
                        if stopper in done:
                            recv.cancel()
                            return
                        event = json.loads(recv.result())
                        if event.get("type") == "ws.hello" and store.reset_if_relay_changed(event["relay_id"]):
                            log.warning("relay identity changed; resyncing from scratch")
                            break
                        apply_event(store, event)
                        on_event(event)
                finally:
                    stopper.cancel()
        except InvalidStatus as e:
            if e.response.status_code == 401:
                raise FatalSyncError("relay rejected the API key") from e
            log.warning("relay refused WebSocket: HTTP %s", e.response.status_code)
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else None
            if code == CLOSE_REPLACED:
                raise FatalSyncError("another agentring daemon connected to this relay with the same API key") from e
            if code == CLOSE_UNAUTHORIZED:
                raise FatalSyncError("relay rejected the API key") from e
            log.warning("connection closed (%s); reconnecting", code)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("cannot reach relay: %s", e)
        if stop.is_set():
            return
        delay = min(backoff, 30.0) * (0.8 + random.random() * 0.4)
        backoff *= 2
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
