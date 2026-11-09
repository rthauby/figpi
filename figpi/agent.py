"""The agent loop: user message -> Ollama -> tool calls -> Ollama ... -> answer.

The conversation is persisted to disk so it survives service restarts and reboots.
Events are emitted through a callback so the same loop drives the web UI, the CLI and tests.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from . import __version__
from .config import Config
from .ollama import Ollama, OllamaError, discover
from .tools import Tool, build_tools, device_model, ip_addresses, memory_text

log = logging.getLogger("figpi.agent")

Event = dict  # {"type": "text"|"thinking"|"tool_call"|"tool_result"|"done"|"error", ...}
Emit = Callable[[Event], None]

BASE_PROMPT = """You are {name}, a small assistant that lives on a {device} (hostname {hostname}, IP {ips}).
You run as a lightweight service on the Pi itself; your thinking happens on an Ollama server in the same network.
You can act on the Pi with the tools you are given. Use them whenever the user asks about the device, its
sensors, pins, files, or wants something done, and report what actually happened. Keep answers short and concrete;
this is a terminal-sized chat. If a tool returns an error, say so plainly. Prefer dedicated tools (gpio, system_status,
read_file) over run_command when they fit. Never invent command output.
Today is {date}. figpi version {version}."""


class Agent:
    def __init__(self, cfg: Config, client: Ollama | None = None, model: str | None = None):
        self.cfg = cfg
        self.client = client
        self.model = model or cfg.ollama.model
        self.tools: list[Tool] = build_tools(cfg)
        self._tools_by_name = {t.name: t for t in self.tools}
        self.lock = threading.Lock()
        self.history_path = Path(cfg.state_dir) / "history.json"
        self.messages: list[dict] = self._load_history()
        self.last_error: str | None = None
        self.capabilities: set[str] = set()

    # -- connection -----------------------------------------------------------

    def connect(self) -> None:
        """Resolve the Ollama URL (scanning the LAN if configured to) and choose a model."""
        if self.client is None:
            url = self.cfg.ollama.url
            if url == "auto":
                log.info("scanning the local network for an Ollama server...")
                url = discover()
                if not url:
                    raise OllamaError(
                        "no Ollama server found on the local network; set ollama.url in figpi.toml "
                        "and make sure Ollama listens on 0.0.0.0 (OLLAMA_HOST=0.0.0.0)"
                    )
                log.info("found Ollama at %s", url)
            self.client = Ollama(url, timeout=self.cfg.ollama.timeout)
        self.model = self.client.pick_model(self.model or self.cfg.ollama.model)
        self.capabilities = self.client.capabilities(self.model)
        log.info("using model %s (capabilities: %s)", self.model, ", ".join(sorted(self.capabilities)) or "unknown")
        if self.capabilities and "tools" not in self.capabilities:
            log.warning("model %s does not advertise tool support; it may not be able to act on the Pi", self.model)

    def ensure_connected(self) -> None:
        if self.client is None or not self.model:
            self.connect()

    def status(self) -> dict:
        return {
            "version": __version__,
            "ollama_url": self.client.base_url if self.client else None,
            "model": self.model or None,
            "capabilities": sorted(self.capabilities),
            "tools": [t.name for t in self.tools],
            "messages": len(self.messages),
            "last_error": self.last_error,
            "hostname": socket.gethostname(),
            "ips": ip_addresses(),
        }

    # -- history ----------------------------------------------------------------

    def _load_history(self) -> list[dict]:
        try:
            data = json.loads(self.history_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self) -> None:
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.history_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.messages, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.history_path)
        except OSError as exc:
            log.warning("could not save history: %s", exc)

    def reset(self) -> None:
        with self.lock:
            self.messages = []
            self._save_history()

    def _window(self) -> list[dict]:
        """Drop the oldest turns beyond the configured window, never splitting a tool exchange."""
        limit = self.cfg.agent.history_messages
        msgs = self.messages
        if len(msgs) <= limit:
            return msgs
        cut = len(msgs) - limit
        while cut < len(msgs) and msgs[cut]["role"] != "user":
            cut += 1
        return msgs[cut:]

    def system_prompt(self) -> str:
        prompt = BASE_PROMPT.format(
            name=self.cfg.agent.name,
            device=device_model(),
            hostname=socket.gethostname(),
            ips=", ".join(ip_addresses()) or "unknown",
            date=time.strftime("%A %Y-%m-%d %H:%M"),
            version=__version__,
        )
        if self.cfg.agent.system_prompt:
            prompt += "\n\n" + self.cfg.agent.system_prompt.strip()
        notes = memory_text(self.cfg)
        if notes:
            prompt += "\n\nThings you remembered earlier:\n" + notes
        return prompt

    # -- the loop ---------------------------------------------------------------

    def run_turn(self, user_text: str, emit: Emit) -> str:
        """Process one user message. Emits events as they happen; returns the final assistant text."""
        with self.lock:
            try:
                self.ensure_connected()
            except OllamaError as exc:
                self.last_error = str(exc)
                emit({"type": "error", "message": str(exc)})
                emit({"type": "done"})
                return ""
            self.messages.append({"role": "user", "content": user_text})
            final_text = ""
            try:
                for _round in range(self.cfg.agent.max_tool_rounds + 1):
                    reply, tool_calls = self._stream_reply(emit)
                    self.messages.append(reply)
                    final_text = reply.get("content", "")
                    if not tool_calls or _round == self.cfg.agent.max_tool_rounds:
                        break
                    for call in tool_calls:
                        self.messages.append(self._run_tool(call, emit))
                self.last_error = None
            except OllamaError as exc:
                self.last_error = str(exc)
                emit({"type": "error", "message": str(exc)})
            finally:
                self._save_history()
                emit({"type": "done"})
            return final_text

    def _stream_reply(self, emit: Emit) -> tuple[dict, list[dict]]:
        messages = [{"role": "system", "content": self.system_prompt()}] + self._window()
        content, thinking, tool_calls = [], [], []
        for chunk in self.client.chat(
            self.model,
            messages,
            tools=[t.spec() for t in self.tools],
            # Ollama rejects `think` for models without the capability, so only send it where it applies.
            think=self.cfg.ollama.think if "thinking" in self.capabilities else None,
            options={"num_ctx": self.cfg.ollama.num_ctx},
            keep_alive=self.cfg.ollama.keep_alive,
        ):
            if chunk.thinking:
                thinking.append(chunk.thinking)
                emit({"type": "thinking", "text": chunk.thinking})
            if chunk.content:
                content.append(chunk.content)
                emit({"type": "text", "text": chunk.content})
            for call in chunk.tool_calls:
                fn = call.get("function") or {}
                tool_calls.append({"function": {"name": fn.get("name", ""), "arguments": fn.get("arguments") or {}}})
            if chunk.done and chunk.stats:
                emit({"type": "stats", **chunk.stats})
        reply: dict = {"role": "assistant", "content": "".join(content)}
        if tool_calls:
            reply["tool_calls"] = tool_calls
        return reply, tool_calls

    def _run_tool(self, call: dict, emit: Emit) -> dict:
        fn = call["function"]
        name, args = fn["name"], fn["arguments"]
        if isinstance(args, str):  # some models emit arguments as a JSON string
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        emit({"type": "tool_call", "name": name, "arguments": args})
        tool = self._tools_by_name.get(name)
        if tool is None:
            result = f"error: unknown tool {name!r}; available: {', '.join(self._tools_by_name)}"
        else:
            started = time.monotonic()
            try:
                result = tool.run(args if isinstance(args, dict) else {})
            except Exception as exc:  # a tool bug must not kill the turn
                log.exception("tool %s failed", name)
                result = f"error: {type(exc).__name__}: {exc}"
            log.info("tool %s took %.1fs", name, time.monotonic() - started)
        emit({"type": "tool_result", "name": name, "result": result})
        return {"role": "tool", "tool_name": name, "content": result}
