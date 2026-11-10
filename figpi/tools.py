"""The agent's hands: a small, auditable set of tools that run on the Pi.

Every tool is a plain function taking a dict of arguments and returning a string.
Tools are grouped; the config decides which groups the model is offered.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config

MAX_OUTPUT = 12_000  # characters returned to the model per tool call


@dataclass
class Tool:
    name: str
    group: str
    description: str
    parameters: dict
    run: Callable[[dict], str]

    def spec(self) -> dict:
        """Ollama / OpenAI style function definition."""
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + f"\n... [{len(text) - limit} characters trimmed] ...\n" + text[-limit // 2 :]


def _sh(cmd: list[str], timeout: float = 10) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (out.stdout + out.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"error: {exc}"


# --- system -----------------------------------------------------------------


def device_model() -> str:
    try:
        return Path("/proc/device-tree/model").read_text().strip("\x00\n")
    except OSError:
        return platform.machine()


def ip_addresses() -> list[str]:
    out = _sh(["ip", "-4", "-o", "addr", "show", "scope", "global"])
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and "/" in parts[3]:
            ips.append(parts[3].split("/")[0])
    if not ips:
        try:
            ips.append(socket.gethostbyname(socket.gethostname()))
        except OSError:
            pass
    return ips


def cpu_temperature() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def system_status(_: dict) -> str:
    vm = shutil.disk_usage("/")
    info = {
        "hostname": socket.gethostname(),
        "device": device_model(),
        "ip_addresses": ip_addresses(),
        "cpu_temperature_c": cpu_temperature(),
        "uptime_s": int(float(Path("/proc/uptime").read_text().split()[0])) if Path("/proc/uptime").exists() else None,
        "load_average": os.getloadavg() if hasattr(os, "getloadavg") else None,
        "disk_free_gb": round(vm.free / 2**30, 2),
        "disk_total_gb": round(vm.total / 2**30, 2),
        "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }
    try:
        mem = dict(line.split(":") for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line)
        info["mem_total_mb"] = int(mem["MemTotal"].split()[0]) // 1024
        info["mem_available_mb"] = int(mem["MemAvailable"].split()[0]) // 1024
    except (OSError, KeyError, ValueError):
        pass
    if shutil.which("vcgencmd"):
        info["throttled"] = _sh(["vcgencmd", "get_throttled"])
    return json.dumps(info, indent=1)


# --- gpio -------------------------------------------------------------------


def gpio(args: dict) -> str:
    """Drive header pins through `pinctrl` (ships with Raspberry Pi OS, works on every Pi from the 3 up, Pi 5 included)."""
    action = str(args.get("action", "")).lower()
    pin = args.get("pin")
    if not shutil.which("pinctrl"):
        return "error: pinctrl not found (is this Raspberry Pi OS?)"
    if action == "list":
        return _clip(_sh(["pinctrl", "get"]))
    try:
        pin = int(pin)
    except (TypeError, ValueError):
        return "error: 'pin' must be a BCM GPIO number (integer)"
    if not 0 <= pin <= 27:
        return "error: pin must be between 0 and 27 (BCM numbering on the 40-pin header)"
    if action == "read":
        return _sh(["pinctrl", "get", str(pin)])
    if action == "write":
        value = args.get("value")
        if value in (1, True, "1", "high", "on", "true"):
            level = "dh"
        elif value in (0, False, "0", "low", "off", "false"):
            level = "dl"
        else:
            return "error: 'value' must be 0/1, high/low or on/off"
        out = _sh(["pinctrl", "set", str(pin), "op", level])
        return out or f"GPIO{pin} set to {'HIGH' if level == 'dh' else 'LOW'}"
    if action == "input":
        pull = {"up": "pu", "down": "pd", "none": "pn"}.get(str(args.get("pull", "none")).lower(), "pn")
        out = _sh(["pinctrl", "set", str(pin), "ip", pull])
        return out or f"GPIO{pin} configured as input (pull {args.get('pull', 'none')})"
    return "error: action must be one of read, write, input, list"


# --- shell ------------------------------------------------------------------


def make_shell(cfg: Config) -> Callable[[dict], str]:
    def run_command(args: dict) -> str:
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return "error: empty command"
        timeout = min(int(args.get("timeout", cfg.tools.shell_timeout) or cfg.tools.shell_timeout), 600)
        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", cmd], capture_output=True, text=True, timeout=timeout, cwd="/", env=_env()
            )
        except subprocess.TimeoutExpired:
            return f"error: command timed out after {timeout}s"
        except OSError as exc:
            return f"error: {exc}"
        body = ""
        if proc.stdout:
            body += proc.stdout
        if proc.stderr:
            body += ("\n[stderr]\n" if body else "[stderr]\n") + proc.stderr
        body = body.strip() or "(no output)"
        return _clip(f"exit code: {proc.returncode}\n{body}")

    return run_command


def _env() -> dict:
    env = dict(os.environ)
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    env["TERM"] = "dumb"
    env["DEBIAN_FRONTEND"] = "noninteractive"
    return env


# --- files ------------------------------------------------------------------


def make_files(cfg: Config) -> tuple[Callable, Callable, Callable]:
    roots = [Path(r).resolve() for r in cfg.tools.file_roots]

    def resolve(raw: str) -> Path:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = Path("/home") / p
        p = p.resolve()
        if roots and not any(p == r or r in p.parents for r in roots):
            raise PermissionError(f"{p} is outside the allowed roots {', '.join(map(str, roots))}")
        return p

    def read_file(args: dict) -> str:
        try:
            p = resolve(str(args.get("path", "")))
            data = p.read_bytes()
        except (OSError, PermissionError) as exc:
            return f"error: {exc}"
        try:
            return _clip(data.decode("utf-8"))
        except UnicodeDecodeError:
            return f"(binary file, {len(data)} bytes)"

    def write_file(args: dict) -> str:
        try:
            p = resolve(str(args.get("path", "")))
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(args.get("content", ""))
            mode = "a" if args.get("append") else "w"
            with open(p, mode, encoding="utf-8") as fh:
                fh.write(content)
        except (OSError, PermissionError) as exc:
            return f"error: {exc}"
        return f"wrote {len(content)} characters to {p}"

    def list_dir(args: dict) -> str:
        try:
            p = resolve(str(args.get("path", "/home")))
            entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
        except (OSError, PermissionError) as exc:
            return f"error: {exc}"
        lines = []
        for e in entries[:500]:
            try:
                lines.append(f"{'d' if e.is_dir() else '-'} {e.stat().st_size:>10} {e.name}")
            except OSError:
                lines.append(f"? {'':>10} {e.name}")
        return _clip("\n".join(lines) or "(empty)")

    return read_file, write_file, list_dir


# --- http -------------------------------------------------------------------


def http_get(args: dict) -> str:
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return "error: url must start with http:// or https://"
    req = urllib.request.Request(url, headers={"User-Agent": "figpi/0.1 (+raspberry pi agent)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            ctype = resp.headers.get("Content-Type", "")
            data = resp.read(512_000)
    except OSError as exc:
        return f"error: {exc}"
    text = data.decode("utf-8", errors="replace")
    if "html" in ctype:
        text = _strip_html(text)
    return _clip(f"[{ctype}]\n{text}")


def _strip_html(html: str) -> str:
    import re

    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&amp;", "&", html)
    html = re.sub(r"&lt;", "<", html)
    html = re.sub(r"&gt;", ">", html)
    return re.sub(r"\s+", " ", html).strip()


# --- memory -----------------------------------------------------------------


def make_memory(cfg: Config) -> tuple[Callable, Callable]:
    path = Path(cfg.state_dir) / "memory.md"

    def read() -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def remember(args: dict) -> str:
        note = str(args.get("note", "")).strip()
        if not note:
            return "error: empty note"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"- {time.strftime('%Y-%m-%d')}: {note}\n")
        return "saved"

    def forget(args: dict) -> str:
        needle = str(args.get("contains", "")).strip()
        lines = read().splitlines()
        if not needle:
            path.write_text("", encoding="utf-8")
            return f"cleared {len(lines)} notes"
        kept = [l for l in lines if needle.lower() not in l.lower()]
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        return f"removed {len(lines) - len(kept)} notes"

    remember.read = read  # type: ignore[attr-defined]
    return remember, forget


# --- registry ---------------------------------------------------------------


def build_tools(cfg: Config) -> list[Tool]:
    """Return the tools enabled by the config, in a stable order."""
    all_tools: list[Tool] = [
        Tool(
            "system_status",
            "system",
            "Report the Pi's hostname, IP addresses, CPU temperature, memory, disk, uptime and throttling state.",
            {"type": "object", "properties": {}},
            system_status,
        ),
        Tool(
            "gpio",
            "gpio",
            "Read or drive a GPIO pin on the 40-pin header (BCM numbering). "
            "action=write sets an output high/low, action=read reads a pin, action=input configures an input "
            "with optional pull (up/down/none), action=list shows every pin.",
            {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["read", "write", "input", "list"]},
                    "pin": {"type": "integer", "description": "BCM GPIO number 0-27"},
                    "value": {"type": "string", "description": "for write: high or low (also 1/0, on/off)"},
                    "pull": {"type": "string", "enum": ["up", "down", "none"]},
                },
                "required": ["action"],
            },
            gpio,
        ),
        Tool(
            "run_command",
            "shell",
            "Run a bash command on the Pi and return its exit code and output. Use for anything the other tools "
            "don't cover: apt, systemctl, journalctl, i2cdetect, camera, network diagnostics.",
            {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer", "description": "seconds, default 60"},
                },
                "required": ["command"],
            },
            make_shell(cfg),
        ),
        Tool(
            "http_get",
            "http",
            "Fetch a URL (HTML is reduced to text). Use it to read web pages or call local network devices.",
            {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            http_get,
        ),
    ]
    read_file, write_file, list_dir = make_files(cfg)
    all_tools += [
        Tool(
            "read_file",
            "files",
            "Read a text file.",
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            read_file,
        ),
        Tool(
            "write_file",
            "files",
            "Write (or append to) a text file, creating parent directories.",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "append": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            write_file,
        ),
        Tool(
            "list_dir",
            "files",
            "List a directory.",
            {"type": "object", "properties": {"path": {"type": "string"}}},
            list_dir,
        ),
    ]
    remember, forget = make_memory(cfg)
    all_tools += [
        Tool(
            "remember",
            "memory",
            "Save a short note to long-term memory (facts about the user, the setup, preferences). "
            "Notes are shown to you at the start of every conversation.",
            {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]},
            remember,
        ),
        Tool(
            "forget",
            "memory",
            "Delete memory notes containing a phrase (or all notes when the phrase is empty).",
            {"type": "object", "properties": {"contains": {"type": "string"}}},
            forget,
        ),
    ]
    enabled = set(cfg.tools.enabled)
    return [t for t in all_tools if t.group in enabled]


def memory_text(cfg: Config) -> str:
    try:
        return (Path(cfg.state_dir) / "memory.md").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
