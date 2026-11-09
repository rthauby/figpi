"""Configuration: a single TOML file on the FAT boot partition, editable from any computer."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_CANDIDATES = (
    os.environ.get("FIGPI_CONFIG", ""),
    "/boot/firmware/figpi.toml",
    "/boot/figpi.toml",
    "./figpi.toml",
)

DEFAULT_STATE_DIR = "/var/lib/figpi"


@dataclass
class OllamaConfig:
    url: str = "auto"  # http://host:11434 or "auto" to scan the LAN for an Ollama server
    model: str = ""  # empty: pick the first installed model that advertises tool support
    think: bool | None = None  # None: leave the model's default; False keeps small models snappy
    num_ctx: int = 8192
    timeout: float = 300.0
    keep_alive: str = "30m"


@dataclass
class WebConfig:
    bind: str = "0.0.0.0"
    port: int = 8080
    password: str = ""  # empty: a random one is generated on first boot and written next to the config


@dataclass
class ToolsConfig:
    enabled: list[str] = field(default_factory=lambda: ["system", "gpio", "shell", "files", "http", "memory"])
    shell_timeout: int = 60
    file_roots: list[str] = field(default_factory=lambda: ["/home", "/tmp", "/var/lib/figpi", "/boot/firmware"])


@dataclass
class AgentConfig:
    name: str = "figpi"
    system_prompt: str = ""  # appended to the built-in prompt
    max_tool_rounds: int = 12
    history_messages: int = 40  # client-side window: oldest messages beyond this are dropped


@dataclass
class WifiConfig:
    ssid: str = ""
    password: str = ""
    country: str = ""


@dataclass
class Config:
    ollama: OllamaConfig = field(default_factory=OllamaConfig)
    web: WebConfig = field(default_factory=WebConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    wifi: WifiConfig = field(default_factory=WifiConfig)
    state_dir: Path = Path(DEFAULT_STATE_DIR)
    source: Path | None = None


def _merge(obj, data: dict) -> None:
    for key, value in data.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


def load(path: str | os.PathLike | None = None) -> Config:
    cfg = Config()
    if path:
        found = Path(path) if Path(path).is_file() else None
        if found is None:
            logging.getLogger("figpi").warning("config %s not found, using defaults", path)
    else:
        found = next((Path(p) for p in CONFIG_CANDIDATES if p and Path(p).is_file()), None)
    if found is not None:
        with open(found, "rb") as fh:
            data = tomllib.load(fh)
        for section, target in (
            ("ollama", cfg.ollama),
            ("web", cfg.web),
            ("tools", cfg.tools),
            ("agent", cfg.agent),
            ("wifi", cfg.wifi),
        ):
            if isinstance(data.get(section), dict):
                _merge(target, data[section])
        if "state_dir" in data:
            cfg.state_dir = Path(data["state_dir"])
        cfg.source = found
    env_url = os.environ.get("OLLAMA_HOST")
    if env_url and cfg.ollama.url == "auto":
        cfg.ollama.url = env_url if "://" in env_url else f"http://{env_url}"
    return cfg
