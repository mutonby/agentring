"""A voice AI on a call: joins the call's LiveKit room and bridges it to a realtime model.

    agent = VoiceAgent(lambda: GeminiLive(api_key), "You are …", tools=[…])
    result = await agent.run(CallInfo(call_id, reason), join, relay=client)

The agent joins while the phone rings so it can greet as soon as someone answers.
Everyone on the call is mixed into one input (group calls), the user can interrupt,
tools run in the background (the call keeps listening while a slow tool works),
and the model can end the call itself (`end_call`). Everything specific to an app —
instructions, tools, names, greeting wording, guards on tools, what to do with the
transcript — is passed in; nothing here knows who is calling or why.
"""

from __future__ import annotations

import asyncio
import difflib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from livekit import rtc

from .backend import (AudioOut, Interrupted, RealtimeBackend, SessionEnding, ToolCalls, ToolResult, TurnComplete,
                      Transcript)
from .tools import Tool, ToolCall, _session, result_text

log = logging.getLogger("agentring.voice")

TERMINAL_STATES = {"missed", "declined", "ended", "canceled"}
END_CALL = "end_call"


@dataclass
class CallInfo:
    call_id: str
    reason: str = ""
    direction: str = "agent_to_user"  # or user_to_agent (the user called the agent)
    device_ids: list[str] | None = None  # a targeted call: private to these phones (messages reach every phone)

    @property
    def inbound(self) -> bool:
        return self.direction == "user_to_agent"


@dataclass
class Prompts:
    """What the session tells the model (as text notes) when something happens on the call.

    Override them to change the wording or language. Placeholders: {names}, {name},
    {others}, {reason}.
    """

    answered: str = ("({names} answered the phone.) Greet them briefly and tell them in one or two sentences "
                     "why you are calling: {reason}")
    incoming: str = "({names} is calling you.) Answer: greet them briefly and ask what they need."
    joined: str = "({name} just joined the call.) Greet them by name and catch them up in one sentence, then carry on."
    left: str = "({name} hung up; still on the call: {others}.)"
    end_call: str = "End the call once the user says goodbye or asks you to hang up (say goodbye first)."
    tool_timeout: str = "Still running after {seconds} s; it carries on in the background. Tell the user."
    someone: str = "the user"
    joiner: str = " and "


@dataclass
class CallResult:
    call_id: str
    answered: bool
    duration_s: int  # from the first answer to the end
    end_reason: str  # no_answer | hung_up | end_call | relay:<state> | max_duration | disconnected | error
    transcript: list[tuple[str, str]] = field(default_factory=list)  # (user|agent, text)
    tools: list[dict[str, Any]] = field(default_factory=list)  # {time, name, args, output, skipped}
    participants: list[str] = field(default_factory=list)  # everyone who was on the call
    fallback_msg_id: str | None = None  # the message sent instead, if nobody answered (M4)


# Called before every tool. Return None to run it, a dict to run it with those args
# instead, or a string to skip it and give the model that string as the result.
BeforeTool = Callable[["CallSession", ToolCall], "None | str | dict[str, Any] | Awaitable[None | str | dict[str, Any]]"]
Instructions = str | Callable[[CallInfo], "str | Awaitable[str]"]


async def _maybe_await(v: Any) -> Any:
    return await v if inspect.isawaitable(v) else v


class VoiceAgent:
    def __init__(
        self,
        backend: Callable[[], RealtimeBackend],
        instructions: Instructions,
        tools: Sequence[Tool] = (),
        *,
        prompts: Prompts | None = None,
        name_of: Callable[[str], str | None] | None = None,
        before_tool: BeforeTool | None = None,
        end_call_tool: bool = True,
        tool_timeout_s: float = 120,
        ring_timeout_s: float = 75,
        max_duration_s: float | None = None,
        empty_room_grace_s: float = 10,
    ):
        self.backend = backend
        self.instructions = instructions
        self.tools = list(tools)
        self.prompts = prompts or Prompts()
        self.name_of = name_of  # LiveKit identity (device_<id>) → spoken name; None = unknown
        self.before_tool = before_tool
        self.end_call_tool = end_call_tool
        self.tool_timeout_s = tool_timeout_s
        self.ring_timeout_s = ring_timeout_s
        self.max_duration_s = max_duration_s
        self.empty_room_grace_s = empty_room_grace_s

    async def run(self, info: CallInfo, join: dict[str, Any], *, relay: Any = None, end_call: bool = True,
                  stop: asyncio.Event | None = None) -> CallResult:
        """Runs one call until it ends. `join` is the JoinInfo from the relay (livekit_url,
        livekit_token). With `relay` (a RelayClient), the session also ends when the relay
        says the call is over, and hangs it up on the relay at the end. Setting `stop` hangs up."""
        session = CallSession(self, info, relay)
        stopper = asyncio.create_task(_on_set(stop, lambda: session.finish("stopped"))) if stop else None
        try:
            await session.run(join["livekit_url"], join["livekit_token"])
        except Exception as e:
            log.exception("call %s failed", info.call_id)
            session.end_reason = session.end_reason or "error"
            session.error = repr(e)
        finally:
            if stopper:
                stopper.cancel()
            await session.stop(end_call=end_call)
        return session.result()


async def _on_set(event: asyncio.Event, fn: Callable[[], None]) -> None:
    await event.wait()
    fn()


class CallSession:
    """One call in progress. Hooks (before_tool) get it as their context."""

    def __init__(self, agent: VoiceAgent, info: CallInfo, relay: Any = None):
        self.agent, self.info, self.relay = agent, info, relay
        self.prompts = agent.prompts
        self.backend: RealtimeBackend = agent.backend()
        self.tools = {t.name: t for t in agent.tools}
        self.room = rtc.Room()
        self.source = rtc.AudioSource(self.backend.output_rate, 1, queue_size_ms=200)
        self.inputs: dict[str, bytearray] = {}  # each participant's audio, waiting to be sent
        self.output = bytearray()  # model speech waiting to play
        self.answered = asyncio.Event()
        self.done = asyncio.Event()
        self.end_reason = ""
        self.error = ""
        self.present: dict[str, str] = {}  # identity → name of everyone on the call now, in order of arrival
        self.everyone: list[str] = []  # names of everyone who was on the call
        self.transcript: list[tuple[str, str]] = []
        self.partial = {"user": "", "agent": ""}  # the turn still being transcribed
        self.tool_log: list[dict[str, Any]] = []
        self.last_user_speech_at = 0.0  # when someone on the call last said something (transcribed)
        self.last_agent_audio_at = 0.0
        self.answered_at: float | None = None
        self._hangup_at = 0.0
        self._empty: asyncio.TimerHandle | None = None
        self._notes: asyncio.Queue[tuple[str, bool, bool]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._streams: list[rtc.AudioStream] = []
        self._frame = self.backend.output_rate // 100 * 2  # 10 ms of PCM16
        self._chunk = self.backend.input_rate // 25 * 2  # 40 ms per send

        self.room.on("participant_connected", self._on_join)
        self.room.on("participant_disconnected", self._on_leave)
        self.room.on("track_subscribed", self._on_track)
        self.room.on("disconnected", lambda reason: self.finish(f"disconnected ({reason})"))

    # ------------------------------------------------------------ for hooks

    def name(self, identity: str) -> str:
        n = self.agent.name_of(identity) if self.agent.name_of else None
        return n or self.prompts.someone

    @property
    def participants(self) -> list[str]:
        return list(dict.fromkeys(self.present.values()))

    def names(self) -> str:
        return self.prompts.joiner.join(self.participants) or self.prompts.someone

    def notify(self, text: str, *, respond: bool = False, trusted: bool = True) -> None:
        """Tell the model something once it finishes its current sentence. `trusted=False`
        when the text comes from outside the session (another program's output)."""
        self._notes.put_nowait((text, respond, trusted))

    def hang_up(self) -> None:
        """End the call after the model finishes what it is saying."""
        self._hangup_at = self._hangup_at or time.time()

    def finish(self, reason: str) -> None:
        if not self.done.is_set():
            self.end_reason = self.end_reason or reason
            log.info("call %s ending: %s", self.info.call_id, self.end_reason)
            self.done.set()

    # ------------------------------------------------------------ room events

    def _on_join(self, p: rtc.RemoteParticipant) -> None:
        name = self.name(p.identity)
        log.info("%s joined (%s)", name, p.identity)
        if self._empty:
            self._empty.cancel()  # came back (network drop, or "join the call")
            self._empty = None
        if p.identity in self.present:
            return  # the same phone reconnecting
        self.present[p.identity] = name
        if name not in self.everyone:
            self.everyone.append(name)
        if self.answered.is_set():
            self.notify(self.prompts.joined.format(name=name, names=self.names(), reason=self.info.reason), respond=True)
        self.answered_at = self.answered_at or time.time()
        self.answered.set()

    def _on_leave(self, p: rtc.RemoteParticipant) -> None:
        if p.identity in self.room.remote_participants:
            return  # its old session: the same phone is already back
        self.inputs.pop(p.identity, None)
        name = self.present.pop(p.identity, None) or self.name(p.identity)
        log.info("%s left; %d still in", name, len(self.room.remote_participants))
        if not self.room.remote_participants:
            # Grace period in case they come back (network drop, "join the call").
            self._empty = asyncio.get_running_loop().call_later(self.agent.empty_room_grace_s, self.finish, "hung_up")
        else:
            self.notify(self.prompts.left.format(name=name, others=self.names(), names=self.names(),
                                                 reason=self.info.reason))

    def _on_track(self, track: rtc.Track, _pub: rtc.RemoteTrackPublication, p: rtc.RemoteParticipant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            self._tasks.append(asyncio.create_task(self._listen(track, p.identity)))

    # ------------------------------------------------------------ audio

    async def _listen(self, track: rtc.Track, identity: str) -> None:
        stream = rtc.AudioStream(track, sample_rate=self.backend.input_rate, num_channels=1)
        self._streams.append(stream)
        buf = self.inputs.setdefault(identity, bytearray())
        async for ev in stream:
            buf += bytes(ev.frame.data.cast("B"))

    def take_mixed_input(self) -> bytes:
        """Up to the longest whole 40 ms chunks waiting, everyone's audio summed (with clipping)."""
        bufs = [b for b in self.inputs.values() if b]
        n = max((len(b) for b in bufs), default=0) // self._chunk * self._chunk
        if not n:
            return b""
        mix = np.zeros(n // 2, dtype=np.int32)
        for b in bufs:
            part = np.frombuffer(bytes(b[:n]).ljust(n, b"\0"), dtype=np.int16)
            del b[:n]
            mix += part
        return np.clip(mix, -32768, 32767).astype(np.int16).tobytes()

    async def _send_loop(self) -> None:
        while True:
            await asyncio.sleep(0.02)
            await self.send_pending_input()

    async def send_pending_input(self) -> None:
        """Sends the waiting input in 40 ms chunks: a backlog (e.g. at the start of a call)
        must not go out as one long chunk, which Phonic rejects (max 40 ms)."""
        pcm = self.take_mixed_input()
        for i in range(0, len(pcm), self._chunk):
            await self.backend.send_audio(pcm[i : i + self._chunk])

    async def _play_loop(self) -> None:
        silence = b"\0" * self._frame
        while True:
            if len(self.output) >= self._frame:
                frame = bytes(self.output[: self._frame])
                del self.output[: self._frame]
            else:
                frame = silence
                if self._hangup_at:
                    now = time.time()
                    said_goodbye = now - self._hangup_at > 2 and now - self.last_agent_audio_at > 1.5
                    if said_goodbye or now - self._hangup_at > 20:
                        self.finish(END_CALL)
            await self.source.capture_frame(rtc.AudioFrame(frame, self.backend.output_rate, 1, self._frame // 2))

    async def _notes_loop(self) -> None:
        # `trusted` is optional in a backend: one written before it existed keeps working.
        provenance = "trusted" in inspect.signature(self.backend.send_text).parameters
        while True:
            text, respond, trusted = await self._notes.get()
            while self.output or time.time() - self.last_agent_audio_at < 0.8:
                await asyncio.sleep(0.2)
            await self.backend.send_text(text, respond=respond, **({"trusted": trusted} if provenance else {}))

    # ------------------------------------------------------------ model

    async def _model_loop(self) -> None:
        async for ev in self.backend.events():
            self.handle_event(ev)
        self.finish("model_closed")

    def handle_event(self, ev: Any) -> None:
        if isinstance(ev, ToolCalls):
            # In its own task: the call keeps listening and talking while a tool works.
            self._tasks.append(asyncio.create_task(self._answer_tools(ev.calls)))
        elif isinstance(ev, AudioOut):
            self.output += ev.pcm
            self.last_agent_audio_at = time.time()
        elif isinstance(ev, Interrupted):
            self.output.clear()
            self.source.clear_queue()
        elif isinstance(ev, Transcript):
            self.partial[ev.role] += ev.text
            if ev.role == "user" and ev.text.strip():
                self.last_user_speech_at = time.time()
        elif isinstance(ev, TurnComplete):
            for role in ("user", "agent"):
                text = self.partial[role].strip()
                if text:
                    self.transcript.append((role, text))
                    log.debug("%s: %s", role, text[:200])  # what was said stays out of production logs
                self.partial[role] = ""
        elif isinstance(ev, SessionEnding):
            log.warning("the model will close the session soon: %s", ev.detail)

    async def _answer_tools(self, calls: list[ToolCall]) -> None:
        results = [ToolResult(c, await self.run_tool(c)) for c in calls]
        try:
            await self.backend.send_tool_results(results)
        except Exception as e:
            log.warning("could not return %s: %s", [r.call.name for r in results], e)

    async def run_tool(self, call: ToolCall) -> str:
        log.info("tool %s %s", call.name, str(call.args)[:200])
        skipped = False
        if call.name == END_CALL and self.agent.end_call_tool:
            self.hang_up()
            output = "Hanging up. If you already said goodbye, say nothing more."
        elif call.name not in self.tools:
            # Voice models sometimes translate or misspell a tool name; point them at the right one so they retry.
            close = difflib.get_close_matches(call.name, list(self.tools), n=1, cutoff=0.5)
            output = (f"No such tool: {call.name}. " + (f"Did you mean {close[0]}? Call it again with that exact name."
                      if close else f"Available tools: {', '.join(self.tools)}."))
        else:
            verdict = await _maybe_await(self.agent.before_tool(self, call)) if self.agent.before_tool else None
            if isinstance(verdict, str):
                output, skipped = verdict, True
            else:
                if isinstance(verdict, dict):
                    call = ToolCall(call.id, call.name, verdict)
                tool = self.tools[call.name]
                timeout = tool.timeout_s or self.agent.tool_timeout_s
                _session.set(self)  # this task's context: the tool (and its thread) can find the call
                try:
                    output = result_text(await asyncio.wait_for(tool.call(call.args), timeout))
                except asyncio.TimeoutError:
                    output = self.prompts.tool_timeout.format(seconds=int(timeout))
                except Exception as e:
                    output = f"Error: {str(e)[:300]}"
        self.tool_log.append({"time": time.strftime("%H:%M:%S"), "name": call.name, "args": call.args,
                              "output": output[:2000], "skipped": skipped})
        return output

    def tool_specs(self) -> list[Tool]:
        specs = list(self.tools.values())
        if self.agent.end_call_tool:
            specs.append(Tool(END_CALL, self.prompts.end_call, lambda: None))
        return specs

    # ------------------------------------------------------------ the call

    async def _watch_relay(self) -> None:
        while not self.done.is_set():
            await asyncio.sleep(3)
            try:
                state = (await asyncio.to_thread(self.relay.get_call, self.info.call_id)).get("state")
            except Exception:
                continue
            if state in TERMINAL_STATES:
                self.finish(f"relay:{state}")

    async def _instructions(self) -> str:
        ins = self.agent.instructions
        return await _maybe_await(ins(self.info)) if callable(ins) else ins

    async def _connect_model(self) -> None:
        await self.backend.connect(await self._instructions(), self.tool_specs())

    async def run(self, url: str, token: str) -> None:
        # Open the model session while it rings (and while joining the room), so it greets
        # as soon as someone answers.
        model = asyncio.create_task(self._connect_model())
        self._tasks.append(model)
        await self.room.connect(url, token, rtc.RoomOptions(auto_subscribe=True))
        track = rtc.LocalAudioTrack.create_audio_track("agent", self.source)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        for p in list(self.room.remote_participants.values()):
            self._on_join(p)  # already there (an inbound call: the caller joined first)
        if self.relay is not None:
            self._tasks.append(asyncio.create_task(self._watch_relay()))
        self._tasks.append(asyncio.create_task(self._play_loop()))  # silence until it speaks
        await model
        waits = [asyncio.create_task(self.answered.wait()), asyncio.create_task(self.done.wait())]
        await asyncio.wait(waits, timeout=self.agent.ring_timeout_s, return_when=asyncio.FIRST_COMPLETED)
        for w in waits:
            w.cancel()
        if not self.answered.is_set():
            self.finish("no_answer")
            return
        if self.done.is_set():
            return
        await asyncio.sleep(0.3)  # let the phone's audio settle so the first word isn't clipped
        first = self.prompts.incoming if self.info.inbound else self.prompts.answered
        await self.backend.send_text(first.format(names=self.names(), reason=self.info.reason), respond=True)
        self._tasks += [asyncio.create_task(self._send_loop()), asyncio.create_task(self._model_loop()),
                        asyncio.create_task(self._notes_loop())]
        limit = self.agent.max_duration_s or ((self.backend.max_session_s - 60) if self.backend.max_session_s else None)
        try:
            await asyncio.wait_for(self.done.wait(), limit)
        except asyncio.TimeoutError:
            self.finish("max_duration")

    async def stop(self, *, end_call: bool = True) -> None:
        self.done.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for s in self._streams:
            try:
                await s.aclose()
            except Exception:
                pass
        for close in (self.backend.close, self.room.disconnect):
            try:
                await close()
            except Exception:
                pass
        if end_call and self.relay is not None:
            try:
                await asyncio.to_thread(self.relay.end_call, self.info.call_id)
            except Exception:
                pass  # already over

    def result(self) -> CallResult:
        for role in ("user", "agent"):  # whatever was still being said
            if self.partial[role].strip():
                self.transcript.append((role, self.partial[role].strip()))
                self.partial[role] = ""
        dur = int(time.time() - self.answered_at) if self.answered_at else 0
        return CallResult(self.info.call_id, self.answered_at is not None, dur, self.end_reason or "stopped",
                          self.transcript, self.tool_log, self.everyone)
