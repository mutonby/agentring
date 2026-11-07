"""Daemon configuration: ~/.agentring/config.yaml for settings, ~/.agentring/.env for secrets.

Environment variables (AGENTRING_RELAY_URL, AGENTRING_API_KEY, AGENTRING_HOME)
override the files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import dotenv_values

from .relay_client import InsecureRelayError, check_relay_url


class ConfigError(Exception):
    pass


def home_dir() -> Path:
    return Path(os.environ.get("AGENTRING_HOME") or Path.home() / ".agentring").expanduser()


@dataclass
class Settings:
    relay_url: str
    api_key: str
    home: Path

    @property
    def db_path(self) -> Path:
        return self.home / "agentring.db"


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return data


def load_settings() -> Settings:
    home = home_dir()
    cfg = _read_yaml(home / "config.yaml")
    secrets = dotenv_values(home / ".env") if (home / ".env").exists() else {}

    relay_url = os.environ.get("AGENTRING_RELAY_URL") or cfg.get("relay_url")
    api_key = os.environ.get("AGENTRING_API_KEY") or secrets.get("AGENTRING_API_KEY")
    if not relay_url or not api_key:
        raise ConfigError("Not configured. Run: agentring init --relay https://<your relay> --api-key ak_...")
    try:
        check_relay_url(str(relay_url))
    except InsecureRelayError as e:
        raise ConfigError(str(e)) from None
    return Settings(relay_url=str(relay_url).rstrip("/"), api_key=str(api_key), home=home)


def write_settings(relay_url: str, api_key: str) -> Path:
    home = home_dir()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)  # holds the API key and the local database
    cfg_path = home / "config.yaml"
    cfg = _read_yaml(cfg_path)
    cfg["relay_url"] = relay_url.rstrip("/")
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False))

    env_path = home / ".env"
    lines = []
    if env_path.exists():
        lines = [line for line in env_path.read_text().splitlines() if not line.startswith("AGENTRING_API_KEY=")]
    lines.append(f"AGENTRING_API_KEY={api_key}")
    # Create it private from the start (no window where it's world-readable).
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    return home


def secret(name: str) -> str | None:
    """A secret from the environment or ~/.agentring/.env (e.g. GEMINI_API_KEY)."""
    if os.environ.get(name):
        return os.environ[name]
    env = home_dir() / ".env"
    return (dotenv_values(env).get(name) or None) if env.exists() else None
