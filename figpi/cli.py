"""Command line: `figpi serve` (what systemd runs), `figpi chat`, `figpi ask`, `figpi status`, `figpi firstboot`."""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .agent import Agent
from .config import Config, load
from .ollama import OllamaError

log = logging.getLogger("figpi")


def _print_event(ev: dict) -> None:
    t = ev.get("type")
    if t == "text":
        sys.stdout.write(ev["text"])
        sys.stdout.flush()
    elif t == "tool_call":
        sys.stdout.write(f"\n\033[2m> {ev['name']} {json.dumps(ev['arguments'])}\033[0m\n")
    elif t == "tool_result":
        preview = ev["result"].strip().splitlines()
        shown = "\n".join(preview[:8]) + ("\n..." if len(preview) > 8 else "")
        sys.stdout.write(f"\033[2m{shown}\033[0m\n")
    elif t == "error":
        sys.stdout.write(f"\n\033[31merror: {ev['message']}\033[0m\n")
    elif t == "done":
        sys.stdout.write("\n")
        sys.stdout.flush()


# -- commands --------------------------------------------------------------------


def cmd_serve(cfg: Config, args) -> int:
    from .server import make_server

    password = cfg.web.password or _generated_password(cfg)
    agent = Agent(cfg)
    try:
        agent.connect()
    except OllamaError as exc:
        # Keep serving: the page shows the problem and offers a retry once Ollama is up.
        agent.last_error = str(exc)
        log.warning("%s (will retry on first message)", exc)
    server = make_server(agent, cfg.web.bind, cfg.web.port, password)
    log.info("figpi %s listening on http://%s:%d (model %s)", __version__, cfg.web.bind, cfg.web.port, agent.model or "?")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_chat(cfg: Config, args) -> int:
    agent = Agent(cfg)
    try:
        agent.connect()
    except OllamaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"figpi {__version__} - {agent.model} via {agent.client.base_url}. Ctrl-D to quit, /reset to forget.")
    while True:
        try:
            text = input("\n\033[1myou>\033[0m ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not text:
            continue
        if text == "/reset":
            agent.reset()
            print("(history cleared)")
            continue
        print(f"\033[1m{cfg.agent.name}>\033[0m ", end="", flush=True)
        agent.run_turn(text, _print_event)


def cmd_ask(cfg: Config, args) -> int:
    agent = Agent(cfg)
    try:
        agent.connect()
    except OllamaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    agent.run_turn(" ".join(args.text), _print_event)
    return 1 if agent.last_error else 0


def cmd_status(cfg: Config, args) -> int:
    agent = Agent(cfg)
    try:
        agent.connect()
        ok = True
    except OllamaError as exc:
        agent.last_error = str(exc)
        ok = False
    print(json.dumps({**agent.status(), "config": str(cfg.source), "ok": ok}, indent=2))
    return 0 if ok else 1


def cmd_firstboot(cfg: Config, args) -> int:
    """Runs once, before the service starts: Wi-Fi from the config file, hostname, generated password."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if cfg.wifi.ssid and shutil.which("nmcli"):
        _setup_wifi(cfg)
    if cfg.agent.name and shutil.which("hostnamectl"):
        current = Path("/etc/hostname").read_text().strip() if Path("/etc/hostname").exists() else ""
        if current in ("", "raspberrypi") and cfg.agent.name != current:
            _run(["hostnamectl", "set-hostname", cfg.agent.name])
            hosts = Path("/etc/hosts")
            if hosts.exists() and cfg.agent.name not in hosts.read_text():
                with open(hosts, "a") as fh:
                    fh.write(f"127.0.1.1\t{cfg.agent.name}\n")
    if not cfg.web.password:
        _generated_password(cfg)
    (cfg.state_dir / ".firstboot-done").touch()
    return 0


def _setup_wifi(cfg: Config) -> None:
    if cfg.wifi.country and shutil.which("raspi-config"):
        _run(["raspi-config", "nonint", "do_wifi_country", cfg.wifi.country])
    _run(["nmcli", "radio", "wifi", "on"])
    _run(["nmcli", "device", "wifi", "rescan"], check=False)
    cmd = ["nmcli", "device", "wifi", "connect", cfg.wifi.ssid]
    if cfg.wifi.password:
        cmd += ["password", cfg.wifi.password]
    _run(cmd, check=False)


def _run(cmd: list[str], check: bool = True) -> None:
    try:
        subprocess.run(cmd, check=check, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        log.warning("%s failed: %s", " ".join(cmd), exc)


def _generated_password(cfg: Config) -> str:
    """No password in figpi.toml: make one once, keep it in the state dir and copy it next to the config."""
    pw_file = cfg.state_dir / "web-password"
    try:
        return pw_file.read_text().strip()
    except OSError:
        pass
    password = "-".join(secrets.token_hex(2) for _ in range(3))
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    pw_file.write_text(password + "\n")
    pw_file.chmod(0o600)
    boot_copy = (cfg.source.parent if cfg.source else Path("/boot/firmware")) / "figpi-password.txt"
    try:
        boot_copy.write_text(
            f"Web UI password for figpi: {password}\n"
            "Set web.password in figpi.toml to choose your own; delete this file afterwards.\n"
        )
    except OSError as exc:
        log.warning("could not write %s: %s", boot_copy, exc)
    log.warning("generated web password: %s (also in %s)", password, boot_copy)
    return password


# -- entry -------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="figpi", description="thin Raspberry Pi agent backed by Ollama")
    parser.add_argument("--config", help="path to figpi.toml (default: /boot/firmware/figpi.toml)")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=f"figpi {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the web UI service (default)")
    sub.add_parser("chat", help="interactive terminal chat")
    p_ask = sub.add_parser("ask", help="one question, print the answer")
    p_ask.add_argument("text", nargs="+")
    sub.add_parser("status", help="show connection, model and tools as JSON")
    sub.add_parser("firstboot", help="one-time setup (run by systemd on first boot)")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    cfg = load(args.config)
    handler = {
        None: cmd_serve,
        "serve": cmd_serve,
        "chat": cmd_chat,
        "ask": cmd_ask,
        "status": cmd_status,
        "firstboot": cmd_firstboot,
    }[args.command]
    return handler(cfg, args)
