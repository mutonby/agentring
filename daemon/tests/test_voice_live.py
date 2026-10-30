"""A whole AI call against a real relay, a real LiveKit and a real voice model, with a simulated phone.

Opt-in (needs a LiveKit server and the provider's key; costs a few cents):

    AGENTRING_TEST_LIVEKIT_URL=ws://127.0.0.1:7880 AGENTRING_TEST_LIVEKIT_KEY=devkey \\
    AGENTRING_TEST_LIVEKIT_SECRET=... GEMINI_API_KEY=... uv run --extra voice pytest tests/test_voice_live.py -s

AGENTRING_TEST_PROVIDER picks another provider (openai, grok, phonic; its key instead of
GEMINI_API_KEY) and AGENTRING_TEST_MODEL another model.

The phone answers, waits for the greeting, asks for the time (the agent must use a
tool to know it), then says goodbye (the agent must hang up). The phone speaks with
macOS `say`, so run it on a Mac.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time

import httpx
import pytest

pytest.importorskip("livekit")

from livekit import rtc  # noqa: E402

from agentring.relay_client import RelayClient  # noqa: E402
from agentring.voice import CallInfo, Prompts, Tool, VoiceAgent  # noqa: E402
from agentring.voice.echo import SAMPLE_RATE, synthesize  # noqa: E402
from agentring.voice.providers import PROVIDERS, resolve_voice  # noqa: E402

from .conftest import API_KEY  # noqa: E402

LK_URL = os.environ.get("AGENTRING_TEST_LIVEKIT_URL")
PROVIDER = PROVIDERS[os.environ.get("AGENTRING_TEST_PROVIDER", "gemini")]
pytestmark = pytest.mark.skipif(
    not (LK_URL and os.environ.get(PROVIDER.key_env) and shutil.which("say")),
    reason=f"needs AGENTRING_TEST_LIVEKIT_URL/KEY/SECRET, {PROVIDER.key_env} and macOS say",
)


@pytest.fixture()
def livekit_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIVEKIT_URL", LK_URL or "")
    monkeypatch.setenv("LIVEKIT_API_KEY", os.environ.get("AGENTRING_TEST_LIVEKIT_KEY", "devkey"))
    monkeypatch.setenv("LIVEKIT_API_SECRET", os.environ.get("AGENTRING_TEST_LIVEKIT_SECRET", ""))


class Phone:
    """Answers like the app: accept → join the room → talk when it's its turn."""

    def __init__(self, relay: str, jwt: str):
        self.relay, self.headers = relay, {"authorization": f"Bearer {jwt}"}
        self.room = rtc.Room()
        self.source = rtc.AudioSource(SAMPLE_RATE, 1)
        self.last_heard = 0.0  # when the agent last made sound
        self.heard_s = 0.0

    async def answer(self, call_id: str) -> None:
        async with httpx.AsyncClient() as h:
            r = await h.post(f"{self.relay}/v1/calls/{call_id}/accept", headers=self.headers)
        r.raise_for_status()
        join = r.json()["join"]

        @self.room.on("track_subscribed")
        def _track(track: rtc.Track, *_: object) -> None:
            asyncio.create_task(self._hear(track))

        await self.room.connect(join["livekit_url"], join["livekit_token"])
        track = rtc.LocalAudioTrack.create_audio_track("mic", self.source)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        asyncio.create_task(self._silence())

    async def _hear(self, track: rtc.Track) -> None:
        import numpy as np

        async for ev in rtc.AudioStream(track, sample_rate=16_000, num_channels=1):
            a = np.frombuffer(bytes(ev.frame.data.cast("B")), dtype=np.int16)
            if a.size and np.abs(a).mean() > 300:
                self.last_heard = time.time()
                self.heard_s += a.size / 16_000

    async def _silence(self) -> None:
        """The only writer to the track: queued speech, else silence."""
        self.pending = bytearray()
        step = SAMPLE_RATE // 100 * 2
        while True:
            chunk = bytes(self.pending[:step]).ljust(step, b"\0")
            del self.pending[:step]
            await self.source.capture_frame(rtc.AudioFrame(chunk, SAMPLE_RATE, 1, step // 2))

    async def wait_agent_done(self, timeout: float = 30) -> None:
        """Until the agent has spoken and then been quiet for 1.5 s."""
        start = time.time()
        while time.time() - start < timeout:
            if self.last_heard and time.time() - self.last_heard > 1.5:
                return
            await asyncio.sleep(0.1)
        raise AssertionError("the agent never finished speaking")

    async def say(self, text: str) -> None:
        self.pending += await asyncio.to_thread(synthesize, text, "Mónica")
        while self.pending:
            await asyncio.sleep(0.05)
        self.last_heard = 0.0


def test_ai_call_end_to_end(livekit_env, relay: str):
    client = RelayClient(relay, API_KEY)
    tok = client.create_pairing_token()
    import json

    qr = json.loads(tok["qr_payload"])
    jwt = httpx.post(f"{relay}/v1/pair", json={"pairing_token": qr["pairing_token"], "device_name": "Test",
                                               "platform": "android"}).json()["device_jwt"]
    device = client.list_devices()[0]["device_id"]

    agent = VoiceAgent(
        resolve_voice(PROVIDER.name, model=os.environ.get("AGENTRING_TEST_MODEL")).factory(),
        "Eres un asistente de pruebas que habla por teléfono en castellano. Frases muy cortas. "
        "No sabes la hora: si te la preguntan, usa la herramienta hora y di exactamente lo que devuelva. "
        "Cuando el usuario se despida, despídete en una frase y usa end_call.",
        [Tool("hora", "La hora actual.", lambda: "Son las quince y cuarenta y dos.")],
        prompts=Prompts(answered="({names} ha descolgado.) Salúdale en una frase y dile que llamas para una prueba: {reason}"),
        name_of=lambda identity: "Víctor" if identity == f"device_{device}" else None,
    )

    async def run():
        created = await asyncio.to_thread(client.create_call, "probar la voz")
        call_id = created["call"]["call_id"]
        call = asyncio.create_task(agent.run(CallInfo(call_id, "probar la voz"), created["join"], relay=client))
        phone = Phone(relay, jwt)
        await asyncio.sleep(2)  # it rings
        await phone.answer(call_id)
        await phone.wait_agent_done()
        greeting_s = phone.heard_s
        await phone.say("¿Qué hora es?")
        await phone.wait_agent_done(45)
        await phone.say("Vale, perfecto. Gracias, adiós.")
        result = await asyncio.wait_for(call, 60)
        await phone.room.disconnect()
        return result, greeting_s, phone.heard_s

    result, greeting_s, heard_s = asyncio.run(run())
    client.close()
    print(result)
    assert result.answered and result.participants == ["Víctor"]
    assert greeting_s > 1, "the phone never heard a greeting"
    assert [t["name"] for t in result.tools][:1] == ["hora"], result.tools
    said = " ".join(t for r, t in result.transcript if r == "agent").lower()
    assert "42" in said or "cuarenta y dos" in said, result.transcript
    assert result.end_reason == "end_call", result.end_reason
    assert heard_s > greeting_s + 1


def test_phone_calls_the_agent(livekit_env, relay: str, agentring_home, tmp_path):
    """`agentring serve --ai` answers a call from the phone, talks and hangs up when told goodbye."""
    import json
    import subprocess
    import sys

    from typer.testing import CliRunner

    from agentring.cli import app

    assert CliRunner().invoke(app, ["init", "--relay", relay, "--api-key", API_KEY]).exit_code == 0
    client = RelayClient(relay, API_KEY)
    qr = json.loads(client.create_pairing_token()["qr_payload"])
    jwt = httpx.post(f"{relay}/v1/pair", json={"pairing_token": qr["pairing_token"], "device_name": "Test",
                                               "platform": "android"}).json()["device_jwt"]
    log_path = tmp_path / "serve.log"
    serve = subprocess.Popen(
        [sys.executable, "-m", "agentring.cli", "-v", "serve", "--ai", "--instructions",
         "Eres un asistente de pruebas por teléfono, en castellano y con frases muy cortas. "
         "Cuando el usuario se despida, despídete y usa end_call."],
        stdout=open(log_path, "w"), stderr=subprocess.STDOUT,
        env={**os.environ, "AGENTRING_HOME": str(agentring_home), "AGENTRING_VOICE_PROVIDER": PROVIDER.name})

    async def run():
        deadline = time.time() + 20
        while not (await asyncio.to_thread(client.status))["agent_online"]:
            assert time.time() < deadline, log_path.read_text()
            await asyncio.sleep(0.3)
        async with httpx.AsyncClient() as h:
            r = await h.post(f"{relay}/v1/calls", json={"reason": "una duda"}, headers={"authorization": f"Bearer {jwt}"})
        r.raise_for_status()
        call_id, join = r.json()["call"]["call_id"], r.json()["join"]
        phone = Phone(relay, jwt)

        @phone.room.on("track_subscribed")
        def _track(track: rtc.Track, *_: object) -> None:
            asyncio.create_task(phone._hear(track))

        await phone.room.connect(join["livekit_url"], join["livekit_token"])
        await phone.room.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("mic", phone.source),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        asyncio.create_task(phone._silence())
        await phone.wait_agent_done(40)
        greeting_s = phone.heard_s
        await phone.say("Nada, era solo para probar. Gracias, adiós.")
        deadline = time.time() + 40
        while (state := (await asyncio.to_thread(client.get_call, call_id))["state"]) not in ("ended", "canceled"):
            assert time.time() < deadline, f"still {state}"
            await asyncio.sleep(0.5)
        await phone.room.disconnect()
        return greeting_s

    try:
        greeting_s = asyncio.run(run())
    finally:
        serve.terminate()
        serve.wait(15)
        client.close()
    out = log_path.read_text()
    print("\n".join(line for line in out.splitlines() if "agentring" in line))
    assert greeting_s > 1, out
    assert "answering call_" in out and "end_call" in out, out
