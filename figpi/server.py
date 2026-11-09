"""A tiny HTTP server (stdlib) serving the chat page and a streaming JSON-lines chat endpoint."""

from __future__ import annotations

import base64
import json
import logging
import queue
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import Agent

log = logging.getLogger("figpi.web")
PAGE = Path(__file__).with_name("web") / "index.html"


def make_server(agent: Agent, bind: str, port: int, password: str) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        server_version = "figpi"
        protocol_version = "HTTP/1.1"

        # -- helpers -------------------------------------------------------

        def log_message(self, fmt, *args):  # route to logging instead of stderr
            log.debug(fmt, *args)

        def _authed(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            token = ""
            if header.startswith("Bearer "):
                token = header[7:].strip()
            elif header.startswith("Basic "):
                try:
                    token = base64.b64decode(header[6:]).decode().split(":", 1)[-1]
                except (ValueError, UnicodeDecodeError):
                    token = ""
            return secrets.compare_digest(token, password)

        def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode())

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 1_000_000:
                raise ValueError("body too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}

        # -- routes --------------------------------------------------------

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(HTTPStatus.OK, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/healthz":
                self._send(HTTPStatus.OK, b"ok", "text/plain")
            elif self.path == "/api/status":
                if not self._authed():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "password required"})
                self._json(HTTPStatus.OK, {**agent.status(), "auth_required": bool(password)})
            elif self.path == "/api/history":
                if not self._authed():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "password required"})
                self._json(HTTPStatus.OK, {"messages": agent.messages})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "password required"})
            if self.path == "/api/reset":
                agent.reset()
                return self._json(HTTPStatus.OK, {"ok": True})
            if self.path == "/api/connect":
                try:
                    agent.client = None if agent.cfg.ollama.url == "auto" else agent.client
                    agent.model = agent.cfg.ollama.model
                    agent.connect()
                    return self._json(HTTPStatus.OK, agent.status())
                except Exception as exc:
                    return self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            if self.path != "/api/chat":
                return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                text = str(self._read_json().get("message", "")).strip()
            except ValueError as exc:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            if not text:
                return self._json(HTTPStatus.BAD_REQUEST, {"error": "empty message"})
            if agent.lock.locked():
                return self._json(HTTPStatus.CONFLICT, {"error": "the agent is busy with another message"})

            # Stream events as newline-delimited JSON while the turn runs in a worker thread.
            events: queue.Queue = queue.Queue()
            worker = threading.Thread(target=agent.run_turn, args=(text, events.put), daemon=True)
            worker.start()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                while True:
                    ev = events.get()
                    line = json.dumps(ev, ensure_ascii=False).encode() + b"\n"
                    self.wfile.write(f"{len(line):x}\r\n".encode() + line + b"\r\n")
                    self.wfile.flush()
                    if ev.get("type") == "done":
                        break
                self.wfile.write(b"0\r\n\r\n")
            except (BrokenPipeError, ConnectionResetError):
                log.info("client went away mid-turn; the turn keeps running")

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server
