"""consult_agent and agents.yaml, with small Python scripts standing in for real agents."""

from __future__ import annotations

import asyncio
import sys
import time

import pytest

from agentring.voice.agents import AgentsConfigError, CommandAgent, Consultations, load_agents, parse_agents
from agentring.voice.tools import _session

PY = sys.executable


def test_parse_agents_file(tmp_path):
    f = tmp_path / "agents.yaml"
    f.write_text("""
agents:
  claude:
    description: Claude Code in the web app repo.
    command: [claude, -p, "{task}"]
    cwd: ~/code/webapp
    env: {FOO: 1}
    timeout_s: 90
mcp:
  - http://127.0.0.1:8787/mcp
  - url: http://127.0.0.1:9000/mcp
    tools: [status]
    headers: {Authorization: Bearer x}
""")
    cfg = load_agents(f)
    (a,) = cfg.agents
    assert (a.name, a.command, a.cwd, a.env, a.timeout_s) == ("claude", ["claude", "-p", "{task}"], "~/code/webapp",
                                                             {"FOO": "1"}, 90)
    assert [(m.url, m.tools, m.headers) for m in cfg.mcp] == [
        ("http://127.0.0.1:8787/mcp", None, {}), ("http://127.0.0.1:9000/mcp", ["status"], {"Authorization": "Bearer x"})]
    assert parse_agents(None).agents == []


@pytest.mark.parametrize("data, error", [
    ({"agents": {"a": {"description": "x", "command": "claude -p '{task}'"}}}, "not a shell string"),
    ({"agents": {"a": {"command": ["x"]}}}, "`description` is required"),
    ({"agents": {"a b": {"description": "x", "command": ["x"]}}}, "use letters"),
    ({"agents": {"a": {"description": "x", "command": []}}}, "non-empty list"),
    ({"agents": [{"name": "a"}]}, "must map"),
    ({"mcp": [{"tools": ["x"]}]}, "needs a `url`"),
    ({"other": 1}, "unknown keys"),
])
def test_parse_agents_rejects(data, error):
    with pytest.raises(AgentsConfigError, match=error):
        parse_agents(data)


def test_task_is_one_argument_never_a_shell():
    a = CommandAgent("t", "x", [PY, "-c", "import sys; print(sys.argv[1:])", "--task={task}"])
    out = asyncio.run(a.ask('rm -rf / ; echo "$HOME" `id`'))
    assert out == "['--task=rm -rf / ; echo \"$HOME\" `id`']"


@pytest.mark.parametrize("command, task, expected", [
    # Whole argument, nothing guarding it: the option stays text.
    (["claude", "-p", "{task}"], "--permission-mode=acceptEdits", ["claude", "-p", "Task: --permission-mode=acceptEdits"]),
    (["agent", "{task}"], "-x", ["agent", "Task: -x"]),
    (["agent", "{task}", "--"], "-x", ["agent", "Task: -x", "--"]),  # the `--` comes too late
    (["agent", "--flag", "{task}"], "---", ["agent", "--flag", "Task: ---"]),
    # Nothing to do: no leading dash, already after `--`, or inside a longer argument.
    (["claude", "-p", "{task}"], "what is -x?", ["claude", "-p", "what is -x?"]),
    (["claude", "-p", "--", "{task}"], "--yolo", ["claude", "-p", "--", "--yolo"]),
    (["opencode", "run", "Task: {task}"], "--yolo", ["opencode", "run", "Task: --yolo"]),
    (["agent", "--message={task}"], "--yolo", ["agent", "--message=--yolo"]),
])
def test_task_is_never_an_option(command, task, expected):
    assert CommandAgent("t", "x", command).argv(task) == (expected, None)


def test_task_with_a_dash_reaches_a_real_parser_as_text():
    code = ("import argparse; p = argparse.ArgumentParser(); p.add_argument('--evil', action='store_true'); "
            "p.add_argument('task'); a = p.parse_args(); print(a.evil, a.task)")
    assert asyncio.run(CommandAgent("t", "x", [PY, "-c", code, "{task}"]).ask("--evil")) == "False Task: --evil"


def test_task_on_stdin_without_placeholder(tmp_path):
    a = CommandAgent("t", "x", [PY, "-c", "import os, sys; print(os.getcwd(), os.environ['X'], sys.stdin.read())"],
                     cwd=str(tmp_path), env={"X": "1"})
    assert asyncio.run(a.ask("hello")) == f"{tmp_path.resolve()} 1 hello"


def test_failure_and_timeout():
    with pytest.raises(RuntimeError, match=r"failed \(exit 3\): boom"):
        asyncio.run(CommandAgent("t", "x", [PY, "-c", "import sys; print('boom', file=sys.stderr); sys.exit(3)"]).ask("q"))
    t0 = time.time()
    with pytest.raises(TimeoutError, match="within 0 s"):
        asyncio.run(CommandAgent("t", "x", [PY, "-c", "import time; time.sleep(30)"], timeout_s=0.5).ask("q"))
    assert time.time() - t0 < 10
    with pytest.raises(OSError):
        asyncio.run(CommandAgent("t", "x", ["/nonexistent/agent"]).ask("q"))


class _FakeSession:
    class info:  # noqa: N801
        call_id = "call_1"
        device_ids = None

    def __init__(self, relay=None):
        self.done = asyncio.Event()
        self.notes: list[tuple[str, bool]] = []
        self.relay = relay

    def notify(self, text, *, respond=False, trusted=True):
        self.notes.append((text, respond))


class _FakeRelay:
    def __init__(self):
        self.messages: list[str] = []

    def post_message(self, text):
        self.messages.append(text)


def _agents(**kw) -> Consultations:
    echo = CommandAgent("echo", "Echoes.", [PY, "-c", "import sys; print('got', sys.argv[1])", "{task}"])
    slow = CommandAgent("slow", "Slow.", [PY, "-c", "import sys, time; time.sleep(0.6); print('late', sys.argv[1])",
                                          "{task}"])
    return Consultations([echo, slow], answer_within_s=0.3, **kw)


def test_consult_answers_in_the_call_when_quick():
    async def run():
        c, s = _agents(), _FakeSession()
        _session.set(s)
        return await c.consult("echo", "status?"), await c.consult("nope", "x"), c.tool(), s.notes

    out, unknown, tool, notes = asyncio.run(run())
    assert "got status?" in out and "echo answered" in out
    assert unknown.startswith("No such agent: nope")
    assert notes == []
    assert tool.name == "consult_agent" and tool.parameters["properties"]["agent"]["enum"] == ["echo", "slow"]
    assert "- echo: Echoes." in tool.description and "- slow: Slow." in tool.description


def test_slow_answer_comes_as_a_note_later():
    async def run():
        c, s = _agents(), _FakeSession()
        _session.set(s)
        first = await c.consult("slow", "deploy")
        busy = await c.consult("slow", "again")
        await c.drain(5)
        return first, busy, s.notes

    first, busy, notes = asyncio.run(run())
    assert "working on it" in first
    assert "still busy" in busy and "deploy" in busy
    assert len(notes) == 1 and "late deploy" in notes[0][0] and notes[0][1] is True


def test_answer_after_the_call_goes_to_the_phone_as_a_message():
    async def run():
        relay = _FakeRelay()
        c, s = _agents(), _FakeSession(relay)
        _session.set(s)
        await c.consult("slow", "backup")
        s.done.set()  # the call ended before the agent answered
        await c.drain(5)
        return s.notes, relay.messages

    notes, messages = asyncio.run(run())
    assert notes == []
    assert len(messages) == 1 and "after the call" in messages[0] and "late backup" in messages[0]


def test_answer_after_a_targeted_call_goes_to_no_phone():
    """A message reaches every paired phone; the call was private to some of them."""
    async def run():
        relay = _FakeRelay()
        c, s = _agents(), _FakeSession(relay)
        s.info = type("Info", (), {"call_id": "call_1", "device_ids": ["dev_A"]})
        _session.set(s)
        await c.consult("slow", "backup")
        s.done.set()
        await c.drain(5)
        return s.notes, relay.messages

    assert asyncio.run(run()) == ([], [])


def test_late_answer_is_marked_as_not_trusted():
    notes = []

    class Session(_FakeSession):
        def notify(self, text, *, respond=False, trusted=True):
            notes.append((respond, trusted))

    async def run():
        c, s = _agents(), Session()
        _session.set(s)
        await c.consult("slow", "x")
        await c.drain(5)

    asyncio.run(run())
    assert notes == [(True, False)]


def test_close_stops_agents_still_working():
    async def run():
        c, s = _agents(), _FakeSession()
        _session.set(s)
        await c.consult("slow", "x")
        await c.aclose()
        return c.pending, s.notes

    pending, notes = asyncio.run(run())
    assert not pending and notes == []


def test_missed_call_message():
    from agentring.voice.quick import missed_call_message

    class Relay:
        def post_message(self, text, *, reply_to=None):
            self.sent = (text, reply_to)
            return {"msg_id": "msg_1"}

    r = Relay()
    assert missed_call_message(r, "call_1", "Deploy blocked", "Need OK for the migration.") == "msg_1"
    assert r.sent == ("Missed call: Deploy blocked\n\nNeed OK for the migration.", "call_1")


def test_cli_agents_ask_reads_the_group_agents_option(tmp_path):
    from typer.testing import CliRunner

    from agentring.cli import app

    f = tmp_path / "agents.yaml"
    f.write_text(f"""
agents:
  echo:
    description: Echoes the task.
    command: [{PY}, -c, "import sys; print('got ' + sys.argv[1])", "{{task}}"]
""")
    for args in (["agents", "--agents", str(f), "ask", "echo", "hi"],
                 ["agents", "ask", "--agents", str(f), "echo", "hi"]):
        r = CliRunner().invoke(app, args)
        assert r.exit_code == 0, r.output
        assert r.output.strip() == "got hi"
