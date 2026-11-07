"""The user's own agents as tools of the voice AI: `consult_agent` and agents.yaml.

    # ~/.agentring/agents.yaml
    agents:
      claude:
        description: Claude Code in my web app's repo (code, CI, deploys).
        command: [claude, -p, --, "{task}"]
        cwd: ~/code/webapp
    mcp:
      - url: http://127.0.0.1:8787/mcp
        tools: [status, logs]

A command agent is run as an argv list, never through a shell: the task comes from a
voice model and is only ever one argument ("{task}" in `command`, or stdin if no
argument has it), and it is never read as an option (see `CommandAgent.argv`). Agents are slow, so `consult_agent` waits a little and otherwise lets
the call go on; the answer is given to the model when it arrives, or sent to the phone
as a message if the call is already over.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .tools import Tool, current_session

log = logging.getLogger("agentring.voice.agents")

TASK = "{task}"
TASK_PREFIX = "Task: "  # keeps a task that starts with "-" from being parsed as an option
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AgentsConfigError(Exception):
    pass


@dataclass
class CommandAgent:
    name: str
    description: str
    command: list[str]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout_s: float = 600

    def argv(self, task: str) -> tuple[list[str], bytes | None]:
        """The argv to run and what to write on stdin (the task, if no argument takes it).

        The task comes from a voice model, so it must not become an option of the agent's CLI:
        as a whole argument with no `--` before it, a task starting with "-" gets TASK_PREFIX.
        After `--`, or inside a longer argument, it is left as it is."""
        if not any(TASK in a for a in self.command):
            return list(self.command), task.encode()
        out: list[str] = []
        options_ended = False
        for a in self.command:
            bare = a == TASK and not options_ended and task.startswith("-")
            out.append(TASK_PREFIX + task if bare else a.replace(TASK, task))
            options_ended = options_ended or a == "--"
        return out, None

    async def ask(self, task: str, *, call_id: str | None = None) -> str:
        """Runs the agent once; its stdout is the answer. Raises on failure or timeout."""
        argv, stdin = self.argv(task)
        env = {**os.environ, **self.env, **({"AGENTRING_CALL_ID": call_id} if call_id else {})}
        cwd = str(Path(self.cwd).expanduser()) if self.cwd else None
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=os.name == "posix")
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), self.timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            await _kill(proc)
            if isinstance(e, asyncio.CancelledError):
                raise
            raise TimeoutError(f"{self.name} gave no answer within {int(self.timeout_s)} s") from None
        if proc.returncode:
            tail = (err or out).decode(errors="replace").strip()[-500:]
            raise RuntimeError(f"{self.name} failed (exit {proc.returncode}): {tail}")
        return out.decode(errors="replace").strip()


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """The agent and everything it started (its own process group on POSIX)."""
    if proc.returncode is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, sig)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(proc.wait(), 5)
            return
        except asyncio.TimeoutError:
            continue


@dataclass
class McpServer:
    url: str
    tools: list[str] | None = None  # allowlist; None = all of them
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class AgentsConfig:
    agents: list[CommandAgent] = field(default_factory=list)
    mcp: list[McpServer] = field(default_factory=list)


def _str_map(value: Any, what: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AgentsConfigError(f"{what} must be a mapping")
    return {str(k): str(v) for k, v in value.items()}


def parse_agents(data: Any) -> AgentsConfig:
    if data is None:
        return AgentsConfig()
    if not isinstance(data, dict):
        raise AgentsConfigError("agents.yaml must be a mapping with `agents` and/or `mcp`")
    unknown = set(data) - {"agents", "mcp"}
    if unknown:
        raise AgentsConfigError(f"unknown keys in agents.yaml: {', '.join(sorted(unknown))}")
    cfg = AgentsConfig()
    agents = data.get("agents") or {}
    if not isinstance(agents, dict):
        raise AgentsConfigError("`agents` must map each agent's name to its settings")
    for name, spec in agents.items():
        name = str(name)
        if not _NAME.match(name):
            raise AgentsConfigError(f"agent name {name!r}: use letters, digits, _ or -")
        if not isinstance(spec, dict):
            raise AgentsConfigError(f"agent {name}: settings must be a mapping")
        command = spec.get("command")
        if isinstance(command, str):
            raise AgentsConfigError(f"agent {name}: `command` must be a list of arguments, not a shell string")
        if not isinstance(command, list) or not command or not all(isinstance(a, (str, int, float)) for a in command):
            raise AgentsConfigError(f"agent {name}: `command` must be a non-empty list of arguments")
        description = spec.get("description")
        if not description:
            raise AgentsConfigError(f"agent {name}: `description` is required (the voice AI picks agents by it)")
        try:
            timeout = float(spec.get("timeout_s", 600))
        except (TypeError, ValueError):
            raise AgentsConfigError(f"agent {name}: `timeout_s` must be a number") from None
        cfg.agents.append(CommandAgent(name, str(description).strip(), [str(a) for a in command],
                                       str(spec["cwd"]) if spec.get("cwd") else None,
                                       _str_map(spec.get("env"), f"agent {name}: `env`"), timeout))
    servers = data.get("mcp") or []
    if not isinstance(servers, list):
        raise AgentsConfigError("`mcp` must be a list of servers")
    for i, spec in enumerate(servers):
        if isinstance(spec, str):
            spec = {"url": spec}
        if not isinstance(spec, dict) or not spec.get("url"):
            raise AgentsConfigError(f"mcp[{i}]: needs a `url`")
        allow = spec.get("tools")
        if allow is not None and not (isinstance(allow, list) and all(isinstance(t, str) for t in allow)):
            raise AgentsConfigError(f"mcp[{i}]: `tools` must be a list of tool names")
        cfg.mcp.append(McpServer(str(spec["url"]), allow, _str_map(spec.get("headers"), f"mcp[{i}]: `headers`")))
    return cfg


def load_agents(path: str | Path) -> AgentsConfig:
    p = Path(path).expanduser()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise AgentsConfigError(f"cannot read {p}: {e.strerror}") from None
    try:
        return parse_agents(yaml.safe_load(text))
    except yaml.YAMLError as e:
        raise AgentsConfigError(f"{p}: {e}") from None


@dataclass
class ConsultPrompts:
    """What the model is told about consulting agents. Placeholders: {agent}, {task}, {answer}, {agents}."""

    description: str = (
        "Ask one of the user's own AI agents to find out or do something, in plain words. Agents can take "
        "minutes: tell the user you're asking, then keep talking; you will be given the answer when it comes. "
        "Agents:\n{agents}"
    )
    task: str = "What you need from the agent: complete and self-contained, it doesn't hear the call."
    working: str = ("{agent} is working on it; its answer will come to you in a note. Tell the user and carry on, "
                    "don't make it up.")
    answer: str = "({agent} answered “{task}”:)\n{answer}\n(Tell the user the gist, briefly.)"
    failed: str = "({agent} could not answer “{task}”: {answer}) Tell the user."
    busy: str = "{agent} is still busy with “{task}”; ask again when that one answers."
    message: str = "{agent} answered “{task}” after the call:\n\n{answer}"


class Consultations:
    """The `consult_agent` tool, and the agents' answers still on their way."""

    def __init__(self, agents: Iterable[CommandAgent], *, answer_within_s: float = 20,
                 max_answer_chars: int = 4000, prompts: ConsultPrompts | None = None):
        self.agents = {a.name: a for a in agents}
        self.answer_within_s = answer_within_s
        self.max_answer_chars = max_answer_chars
        self.prompts = prompts or ConsultPrompts()
        self.pending: set[asyncio.Task] = set()
        self._busy: dict[str, str] = {}  # agent → task it is on (one at a time: they may edit the same files)

    def tool(self) -> Tool:
        agents = "\n".join(f"- {a.name}: {a.description}" for a in self.agents.values())
        schema = {"type": "object", "properties": {
            "agent": {"type": "string", "enum": list(self.agents), "description": "Which agent."},
            "task": {"type": "string", "description": self.prompts.task}}, "required": ["agent", "task"]}
        # The handler bounds its own wait (answer_within_s); the session's timeout is only a backstop.
        return Tool("consult_agent", self.prompts.description.format(agents=agents), self.consult, schema,
                    timeout_s=self.answer_within_s + 30)

    def _clip(self, text: str) -> str:
        text = text.strip() or "(no output)"
        return text if len(text) <= self.max_answer_chars else "…" + text[-self.max_answer_chars:]

    def _track(self, task: asyncio.Task) -> asyncio.Task:
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)
        return task

    async def consult(self, agent: str, task: str) -> str:
        a = self.agents.get(agent)
        if a is None:
            return f"No such agent: {agent}. Agents: {', '.join(self.agents)}"
        if agent in self._busy:
            return self.prompts.busy.format(agent=agent, task=self._busy[agent])
        session = current_session()
        self._busy[agent] = task
        job = self._track(asyncio.get_running_loop().create_task(self._ask(a, task, session)))
        try:
            done, _ = await asyncio.wait({job}, timeout=self.answer_within_s)
        except asyncio.CancelledError:  # the call is ending: the answer still goes somewhere
            self._track(asyncio.get_running_loop().create_task(self._deliver_later(job, session, agent, task)))
            raise
        if done:
            ok, text = job.result()
            return (self.prompts.answer if ok else self.prompts.failed).format(agent=agent, task=task, answer=text)
        self._track(asyncio.get_running_loop().create_task(self._deliver_later(job, session, agent, task)))
        return self.prompts.working.format(agent=agent, task=task)

    async def _ask(self, a: CommandAgent, task: str, session: Any) -> tuple[bool, str]:
        log.info("asking %s: %s", a.name, task[:200])
        try:
            ok, text = True, self._clip(await a.ask(task, call_id=session.info.call_id if session else None))
        except Exception as e:
            ok, text = False, str(e)[:500]
        finally:
            self._busy.pop(a.name, None)
        log.info("%s %s: %s", a.name, "answered" if ok else "failed", text[:200])
        return ok, text

    async def _deliver_later(self, job: asyncio.Task, session: Any, agent: str, task: str) -> None:
        ok, text = await job
        if session is not None:
            await self._deliver(session, agent, task, ok, text)

    async def _deliver(self, session: Any, agent: str, task: str, ok: bool, text: str) -> None:
        if not session.done.is_set():
            fmt = self.prompts.answer if ok else self.prompts.failed
            session.notify(fmt.format(agent=agent, task=task, answer=text), respond=True, trusted=False)
        elif ok and session.relay is not None and session.info.device_ids is None:
            # The call is over: the answer goes to the phone as a message. Not after a targeted
            # call: messages have no audience, every paired phone would read what it was private from.
            msg = self.prompts.message.format(agent=agent, task=task, answer=text)
            try:
                await asyncio.to_thread(session.relay.post_message, msg)
            except Exception as e:
                log.warning("could not send %s's answer as a message: %s", agent, e)

    async def drain(self, timeout: float | None = None) -> None:
        """Waits for answers still on their way (they are delivered as messages)."""
        if self.pending:
            await asyncio.wait(set(self.pending), timeout=timeout)

    async def aclose(self) -> None:
        for t in list(self.pending):
            t.cancel()
        await asyncio.gather(*self.pending, return_exceptions=True)
