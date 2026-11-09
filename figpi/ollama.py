"""Minimal Ollama HTTP client (stdlib only): streaming chat with tool calls, model listing, LAN discovery."""

from __future__ import annotations

import concurrent.futures
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterator

DEFAULT_PORT = 11434


class OllamaError(RuntimeError):
    pass


@dataclass
class Chunk:
    """One streamed line of /api/chat."""

    content: str = ""
    thinking: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    done: bool = False
    done_reason: str | None = None
    stats: dict = field(default_factory=dict)


class Ollama:
    def __init__(self, base_url: str, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- plumbing ------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            raise OllamaError(f"Ollama returned HTTP {exc.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise OllamaError(f"cannot reach Ollama at {self.base_url}: {exc.reason if hasattr(exc, 'reason') else exc}") from None

    def _json(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        with self._request(method, path, body, timeout) as resp:
            return json.loads(resp.read().decode())

    # -- public API ----------------------------------------------------------

    def version(self) -> str:
        return str(self._json("GET", "/api/version", timeout=5).get("version", "?"))

    def models(self) -> list[dict]:
        return list(self._json("GET", "/api/tags", timeout=10).get("models", []))

    def show(self, model: str) -> dict:
        return self._json("POST", "/api/show", {"model": model}, timeout=20)

    def capabilities(self, model: str) -> set[str]:
        try:
            return set(self.show(model).get("capabilities", []))
        except OllamaError:
            return set()

    def pick_model(self, preferred: str = "") -> str:
        """Use `preferred` if given; otherwise the first installed model that advertises tool support."""
        models = [m.get("model") or m.get("name") for m in self.models()]
        if not models:
            raise OllamaError("the Ollama server has no models installed (run `ollama pull <model>` on it)")
        if preferred:
            names = {m for m in models} | {m.split(":")[0] for m in models}
            if preferred in names or any(m.startswith(preferred) for m in models):
                return preferred
            raise OllamaError(f"model {preferred!r} is not installed on the Ollama server; installed: {', '.join(models)}")
        for m in models:
            if "tools" in self.capabilities(m):
                return m
        return models[0]

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        think: bool | None = None,
        options: dict | None = None,
        keep_alive: str | None = None,
    ) -> Iterator[Chunk]:
        body: dict = {"model": model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        if think is not None:
            body["think"] = think
        if options:
            body["options"] = options
        if keep_alive:
            body["keep_alive"] = keep_alive
        with self._request("POST", "/api/chat", body) as resp:
            for raw in resp:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                if "error" in obj:
                    raise OllamaError(str(obj["error"]))
                msg = obj.get("message") or {}
                yield Chunk(
                    content=msg.get("content") or "",
                    thinking=msg.get("thinking") or "",
                    tool_calls=list(msg.get("tool_calls") or []),
                    done=bool(obj.get("done")),
                    done_reason=obj.get("done_reason"),
                    stats={k: v for k, v in obj.items() if k.endswith(("_count", "_duration"))},
                )


# -- discovery ----------------------------------------------------------------


def _local_subnets() -> list[str]:
    """/24 prefixes of this machine's IPv4 addresses, e.g. ['192.168.1']."""
    prefixes: list[str] = []
    try:
        import subprocess

        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True)
        for line in out.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and "/" in parts[3]:
                prefixes.append(".".join(parts[3].split("/")[0].split(".")[:3]))
    except OSError:
        pass
    if not prefixes:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            prefixes.append(".".join(s.getsockname()[0].split(".")[:3]))
            s.close()
        except OSError:
            pass
    return list(dict.fromkeys(prefixes))


def _probe(host: str, port: int, timeout: float) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return None
    url = f"http://{host}:{port}"
    try:
        Ollama(url).version()
        return url
    except OllamaError:
        return None


def discover(port: int = DEFAULT_PORT, timeout: float = 0.4, candidates: list[str] | None = None) -> str | None:
    """Find an Ollama server on the local /24 networks. Returns its base URL or None."""
    hosts = candidates
    if hosts is None:
        hosts = [f"{p}.{i}" for p in _local_subnets() for i in range(1, 255)]
    if not hosts:
        return None
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as pool:
        for url in pool.map(lambda h: _probe(h, port, timeout), hosts):
            if url:
                pool.shutdown(wait=False, cancel_futures=True)
                return url
    return None
