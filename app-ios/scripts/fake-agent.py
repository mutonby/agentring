"""A test agent for the iOS UI tests: stays online, answers calls from the phone,
greets and echoes in the LiveKit room, and when the call ends tells the phone how
much audio it got from it ("test agent heard N audio frames").

Run with the daemon's environment:
  uv run --project daemon --extra voice python app-ios/scripts/fake-agent.py <relay_url> <api_key>
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

from livekit import rtc

from agentring.relay_client import RelayClient
from agentring.store import Store
from agentring.sync import run_ws
from agentring.voice import accept_incoming, is_incoming_call
from agentring.voice.echo import EchoWorker, synthesize

log = logging.getLogger("fake-agent")


async def answer(client: RelayClient, event: dict[str, Any]) -> None:
    accepted = await asyncio.to_thread(accept_incoming, client, event)
    if not accepted:
        return
    info, join = accepted
    log.info("answered %s", info.call_id)
    left = asyncio.Event()
    worker = EchoWorker(synthesize("Hello from the test agent."), left)
    frames = 0

    @worker.room.on("track_subscribed")
    def _count(track: rtc.Track, _pub: Any, _p: Any) -> None:
        async def count() -> None:
            nonlocal frames
            async for _ in rtc.AudioStream(track, sample_rate=48_000, num_channels=1):
                frames += 1
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            asyncio.create_task(count())

    await worker.start(join["livekit_url"], join["livekit_token"])
    try:
        await asyncio.wait_for(left.wait(), timeout=120)
    except asyncio.TimeoutError:
        pass
    await worker.stop()
    await asyncio.to_thread(client.end_call, info.call_id)
    log.info("call %s over, %d audio frames from the phone", info.call_id, frames)
    await asyncio.to_thread(client.post_message, f"test agent heard {frames} audio frames")


async def main(relay_url: str, api_key: str) -> None:
    client = RelayClient(relay_url, api_key)
    store = Store(Path(tempfile.mkdtemp()) / "agent.db")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_event(e: dict[str, Any]) -> None:
        if e.get("type") == "call.invite" and is_incoming_call(e):
            loop.create_task(answer(client, e))

    log.info("test agent online at %s", relay_url)
    await run_ws(client, api_key, store, on_event, stop=stop)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    asyncio.run(main(sys.argv[1], sys.argv[2]))
