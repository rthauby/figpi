import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from figpi.config import Config


class FakeOllama:
    """Scripted Ollama: each /api/chat call pops the next reply from `script`.

    A reply is either a string (streamed as text chunks) or a list of tool calls
    [{"name": ..., "arguments": {...}}]. Requests are recorded for assertions.
    """

    def __init__(self):
        self.script: list = []
        self.requests: list[dict] = []
        self.models = [{"name": "tiny:latest", "model": "tiny:latest"}, {"name": "big:latest", "model": "big:latest"}]
        self.capabilities = {"tiny:latest": ["completion"], "big:latest": ["completion", "tools"]}
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/version":
                    return self._json({"version": "0.99.0"})
                if self.path == "/api/tags":
                    return self._json({"models": fake.models})
                self._json({"error": "nope"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/show":
                    return self._json({"capabilities": fake.capabilities.get(body.get("model"), [])})
                if self.path != "/api/chat":
                    return self._json({"error": "nope"}, 404)
                fake.requests.append(body)
                if not fake.script:
                    return self._json({"error": "script exhausted"}, 500)
                reply = fake.script.pop(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def line(obj):
                    data = json.dumps(obj).encode() + b"\n"
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")

                if isinstance(reply, str):
                    for i in range(0, len(reply), 7):
                        line({"model": body["model"], "message": {"role": "assistant", "content": reply[i : i + 7]}, "done": False})
                else:
                    calls = [{"function": {"name": c["name"], "arguments": c["arguments"]}} for c in reply]
                    line({"model": body["model"], "message": {"role": "assistant", "content": "", "tool_calls": calls}, "done": False})
                line({"model": body["model"], "message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop", "eval_count": 5, "eval_duration": 1_000_000_000})
                self.wfile.write(b"0\r\n\r\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def fake_ollama():
    f = FakeOllama()
    yield f
    f.close()


@pytest.fixture
def cfg(tmp_path, fake_ollama):
    c = Config()
    c.state_dir = tmp_path / "state"
    c.ollama.url = fake_ollama.url
    c.tools.file_roots = [str(tmp_path)]
    c.tools.enabled = ["system", "files", "memory", "shell"]
    return c
