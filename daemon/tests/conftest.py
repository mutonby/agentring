"""Starts a real relay (relay/dist) on a free port for end-to-end tests."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
RELAY_DIST = ROOT / "relay" / "dist" / "main.js"
API_KEY = "ak_e2e_test_key"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def relay(tmp_path: Path):
    if not RELAY_DIST.exists():
        pytest.skip("relay not built (cd relay && npm run build)")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "AGENTRING_PUBLIC_URL": url,
        "AGENTRING_INSECURE_DEV": "1",
        "AGENTRING_DATA_DIR": str(tmp_path / "relay-data"),
        "AGENTRING_API_KEY": API_KEY,
        "AGENTRING_AGENT_NAME": "E2E",
    }
    proc = subprocess.Popen(
        ["node", "--disable-warning=ExperimentalWarning", str(RELAY_DIST)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("relay did not start: " + proc.stdout.read().decode())
    yield url
    proc.terminate()
    proc.wait(5)


@pytest.fixture()
def agentring_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("AGENTRING_HOME", str(home))
    monkeypatch.delenv("AGENTRING_RELAY_URL", raising=False)
    monkeypatch.delenv("AGENTRING_API_KEY", raising=False)
    return home


HUB_ADMIN_TOKEN = "hub-admin-token-" + "y" * 32


@pytest.fixture()
def hub(tmp_path: Path):
    """A real relay in hub mode (docs/cloud.md); yields its URL. Tenants come from the admin API."""
    if not RELAY_DIST.exists():
        pytest.skip("relay not built (cd relay && npm run build)")
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("AGENTRING_")},
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "AGENTRING_HUB": "1",
        "AGENTRING_HUB_ADMIN_TOKEN": HUB_ADMIN_TOKEN,
        "AGENTRING_PUBLIC_URL": url,
        "AGENTRING_INSECURE_DEV": "1",
        "AGENTRING_DATA_DIR": str(tmp_path / "hub-data"),
    }
    proc = subprocess.Popen(
        ["node", "--disable-warning=ExperimentalWarning", str(RELAY_DIST)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/healthz").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        proc.kill()
        raise RuntimeError("hub did not start: " + proc.stdout.read().decode())
    yield url
    proc.terminate()
    proc.wait(5)
