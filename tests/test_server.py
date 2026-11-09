import json
import threading
import urllib.error
import urllib.request

import pytest

from figpi.agent import Agent
from figpi.server import make_server


@pytest.fixture
def web(cfg, fake_ollama):
    agent = Agent(cfg)
    srv = make_server(agent, "127.0.0.1", 0, "s3cret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", fake_ollama
    srv.shutdown()


def call(url, path, body=None, token="s3cret"):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=10)


def test_page_and_health(web):
    url, _ = web
    assert b"<title>figpi</title>" in urllib.request.urlopen(url + "/").read()
    assert urllib.request.urlopen(url + "/healthz").read() == b"ok"


def test_auth_required(web):
    url, _ = web
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(url, "/api/status", token="wrong")
    assert exc.value.code == 401


def test_chat_streams_ndjson(web):
    url, fake = web
    fake.script = ["pong"]
    with call(url, "/api/chat", {"message": "ping"}) as resp:
        assert resp.headers["Content-Type"] == "application/x-ndjson"
        events = [json.loads(l) for l in resp.read().decode().splitlines()]
    assert "".join(e["text"] for e in events if e["type"] == "text") == "pong"
    assert events[-1]["type"] == "done"
    hist = json.loads(call(url, "/api/history").read())["messages"]
    assert [m["role"] for m in hist] == ["user", "assistant"]
    status = json.loads(call(url, "/api/status").read())
    assert status["model"] == "big:latest" and status["auth_required"] is True
    call(url, "/api/reset", {})
    assert json.loads(call(url, "/api/history").read())["messages"] == []


def test_empty_message_rejected(web):
    url, _ = web
    with pytest.raises(urllib.error.HTTPError) as exc:
        call(url, "/api/chat", {"message": "  "})
    assert exc.value.code == 400
