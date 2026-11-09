"""`agentring connect`: MCP entry, skill and agents.yaml for OpenClaw and Hermes."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from agentring import connect as c
from agentring.cli import app

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated agentring / Hermes / OpenClaw homes and fake harness binaries on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("openclaw", "hermes"):
        p = bindir / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTRING_HOME", str(tmp_path / "agentring"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "openclaw"))
    monkeypatch.delenv("AGENTRING_API_KEY", raising=False)
    return tmp_path


class FakeOpenClaw:
    """Records `openclaw` invocations and keeps the saved MCP server like the real CLI."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.calls: list[list[str]] = []
        self.server: dict | None = None

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        args = argv[1:]
        if args[:2] == ["mcp", "show"]:
            if self.server is None:
                return subprocess.CompletedProcess(argv, 1, "", "not found")
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.server), "")
        if args[:2] == ["mcp", "set"]:
            self.server = json.loads(args[3])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if args[:2] == ["skills", "install"]:
            src = Path(args[2])
            dest = self.state / "skills" / args[args.index("--as") + 1]
            for f in src.rglob("*"):
                if f.is_file():
                    (dest / f.relative_to(src)).parent.mkdir(parents=True, exist_ok=True)
                    (dest / f.relative_to(src)).write_bytes(f.read_bytes())
            (dest / ".openclaw").mkdir(exist_ok=True)  # the real CLI records where the skill came from
            (dest / ".openclaw" / "source-origin.json").write_text("{}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 2, "", "unexpected")


def test_packaged_skill_matches_repo_skill() -> None:
    """skills/agentring (published to ClawHub, GitHub installs) and the copy `connect` installs."""
    repo = {p.relative_to(ROOT / "skills/agentring").as_posix(): p.read_bytes()
            for p in (ROOT / "skills/agentring").rglob("*") if p.is_file()}
    assert repo == c.skill_files()
    assert "SKILL.md" in repo and "references/setup.md" in repo


def test_skill_text_passes_skill_scanners() -> None:
    """Hermes' skills guard blocks a community skill that points the agent at a harness's
    config or .env files (it reads like exfiltration), so the skill sends the agent to
    `agentring connect` and to the web pages for manual steps instead."""
    banned = [".hermes/", ".openclaw/", "hermes config set", "config.yaml", "openclaw.json", "AGENTS.md",
              "agents.md"]
    for rel, data in c.skill_files().items():
        text = data.decode().lower()
        for word in banned:
            assert word.lower() not in text, f"{rel} mentions {word!r}"


def test_mcp_entry_has_long_timeout_and_absolute_command(homes: Path) -> None:
    oc = c.mcp_entry(c.HARNESSES["openclaw"], exe="/opt/bin/agentring")
    assert oc == {"command": "/opt/bin/agentring", "args": ["mcp", "--agent-name", "OpenClaw"],
                  "env": {"AGENTRING_HOME": str(homes / "agentring")}, "requestTimeoutMs": 1_800_000}
    he = c.mcp_entry(c.HARNESSES["hermes"], url="https://agentring.io/mcp")
    assert he == {"url": "https://agentring.io/mcp", "headers": {"Authorization": "Bearer ${AGENTRING_API_KEY}"},
                  "timeout": 1800}
    assert c.mcp_entry(c.HARNESSES["openclaw"], url="https://x.test/mcp")["transport"] == "streamable-http"


def test_hermes_local_is_idempotent_and_keeps_config(homes: Path) -> None:
    cfg = homes / "hermes" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("model: {default: nous/hermes-5}\nmcp_servers:\n  github: {command: npx}\n")
    agents = homes / "agentring" / "agents.yaml"
    agents.parent.mkdir()
    agents.write_text("# my agents\nagents:\n  claude:\n    description: Claude\n    command: [claude, -p, \"{task}\"]\n")

    steps = c.connect("hermes")
    assert [s.status for s in steps] == ["added", "added", "added"]
    data = yaml.safe_load(cfg.read_text())
    assert data["model"] == {"default": "nous/hermes-5"}
    assert data["mcp_servers"]["github"] == {"command": "npx"}
    assert data["mcp_servers"]["agentring"]["timeout"] == 1800
    assert (homes / "hermes" / "config.yaml.bak-agentring").exists()
    assert (homes / "hermes" / "skills" / "agentring" / "references" / "setup.md").exists()

    text = agents.read_text()
    assert text.startswith("# my agents\n")  # appended, comments kept
    loaded = yaml.safe_load(text)["agents"]
    assert set(loaded) == {"claude", "hermes"}
    assert loaded["hermes"]["command"][0].endswith("/hermes")
    assert loaded["hermes"]["command"][1:] == ["-z", "Task: {task}"]

    again = c.connect("hermes")
    assert [s.status for s in again] == ["unchanged", "unchanged", "unchanged"]


def test_agents_yaml_created_and_loadable(homes: Path) -> None:
    from agentring.voice.agents import load_agents

    c.connect("hermes", skill=False)
    cfg = load_agents(str(homes / "agentring" / "agents.yaml"))
    assert [a.name for a in cfg.agents] == ["hermes"]


def test_openclaw_local_uses_its_cli(homes: Path) -> None:
    fake = FakeOpenClaw(homes / "openclaw")
    steps = c.connect("openclaw", run=fake)
    assert [s.status for s in steps] == ["added", "added", "added"]
    assert fake.server["requestTimeoutMs"] == 1_800_000
    assert fake.server["args"] == ["mcp", "--agent-name", "OpenClaw"]
    install = next(a for a in fake.calls if a[1:3] == ["skills", "install"])
    assert install[-4:] == ["--as", "agentring", "--global", "--force"]
    assert (homes / "openclaw" / "skills" / "agentring" / "SKILL.md").exists()

    spec = yaml.safe_load((homes / "agentring" / "agents.yaml").read_text())["agents"]["openclaw"]
    script = spec["command"]
    assert script[:2] == ["sh", "-c"] and script[3].endswith("/openclaw") and script[4] == "{task}"
    assert "$1" in script[2] and "{task}" not in script[2]  # the task never enters the shell script

    fake.calls.clear()
    assert [s.status for s in c.connect("openclaw", run=fake)] == ["unchanged", "unchanged", "unchanged"]
    assert not [a for a in fake.calls if a[1:3] in (["mcp", "set"], ["skills", "install"])]


def test_remote_puts_key_in_harness_env_not_config(homes: Path) -> None:
    steps = c.connect("hermes", url="https://agentring.io/mcp", api_key="ak_secret")
    assert [s.what for s in steps] == ["API key", "MCP server", "skill", "consult from a call"]
    assert steps[-1].status == "skipped"
    env = homes / "hermes" / ".env"
    assert "AGENTRING_API_KEY=ak_secret" in env.read_text()
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert "ak_secret" not in (homes / "hermes" / "config.yaml").read_text()
    assert not (homes / "agentring" / "agents.yaml").exists()

    fake = FakeOpenClaw(homes / "openclaw")
    c.connect("openclaw", url="https://agentring.io/mcp", api_key="ak_secret", run=fake)
    assert "ak_secret" not in json.dumps(fake.server)
    assert (homes / "openclaw" / ".env").read_text() == "AGENTRING_API_KEY=ak_secret\n"


def test_env_file_keeps_other_lines(homes: Path) -> None:
    env = homes / "hermes" / ".env"
    env.parent.mkdir()
    env.write_text("OPENROUTER_API_KEY=sk-or\nAGENTRING_API_KEY=ak_old\n")
    assert c.set_env_var(env, "AGENTRING_API_KEY", "ak_new", dry_run=False) == "updated"
    assert env.read_text() == "OPENROUTER_API_KEY=sk-or\nAGENTRING_API_KEY=ak_new\n"


def test_dry_run_changes_nothing(homes: Path) -> None:
    fake = FakeOpenClaw(homes / "openclaw")
    steps = c.connect("openclaw", dry_run=True, run=fake)
    assert [s.status for s in steps] == ["would add", "would add", "would add"]
    assert fake.server is None
    assert not (homes / "openclaw").exists() and not (homes / "agentring").exists()
    c.connect("hermes", url="https://agentring.io/mcp", api_key="ak_x", dry_run=True)
    assert not (homes / "hermes").exists()


def test_flow_style_agents_yaml_is_rewritten_not_broken(homes: Path) -> None:
    agents = homes / "agentring" / "agents.yaml"
    agents.parent.mkdir()
    agents.write_text('agents: {claude: {description: C, command: [claude, -p, "{task}"]}}\n')
    c.connect("hermes", skill=False)
    assert set(yaml.safe_load(agents.read_text())["agents"]) == {"claude", "hermes"}


def test_missing_binary_changes_nothing(homes: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(c.ConnectError, match="not on PATH"):
        c.connect("hermes")
    assert not (homes / "hermes").exists()


def test_errors(homes: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(c.ConnectError, match="unknown harness"):
        c.connect("nanobot")
    with pytest.raises(c.ConnectError, match="https"):
        c.connect("hermes", url="http://agentring.io/mcp", api_key="ak_x")
    with pytest.raises(c.ConnectError, match="API key"):
        c.connect("hermes", url="https://agentring.io/mcp", api_key=None)
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(c.ConnectError, match="not on PATH"):
        c.connect("openclaw")


def test_cli(homes: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = CliRunner().invoke(app, ["connect", "hermes", "--url", "https://agentring.io/mcp"])
    assert r.exit_code == 1 and "AGENTRING_API_KEY" in r.output
    monkeypatch.setenv("AGENTRING_API_KEY", "ak_cli")
    r = CliRunner().invoke(app, ["connect", "hermes", "--url", "https://agentring.io/mcp", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert [s["status"] for s in out["steps"]] == ["added", "added", "added", "skipped"]
