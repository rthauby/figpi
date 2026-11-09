import json

from figpi.agent import Agent
from figpi.ollama import Ollama, OllamaError


def collect(agent, text):
    events = []
    out = agent.run_turn(text, events.append)
    return out, events


def test_picks_tool_capable_model(fake_ollama):
    assert Ollama(fake_ollama.url).pick_model() == "big:latest"
    assert Ollama(fake_ollama.url).pick_model("tiny") == "tiny"
    try:
        Ollama(fake_ollama.url).pick_model("nope")
    except OllamaError as exc:
        assert "not installed" in str(exc)
    else:
        raise AssertionError


def test_plain_reply_streams_and_persists(cfg, fake_ollama):
    fake_ollama.script = ["Hello from the Pi!"]
    agent = Agent(cfg)
    out, events = collect(agent, "hi")
    assert out == "Hello from the Pi!"
    assert "".join(e["text"] for e in events if e["type"] == "text") == out
    assert events[-1]["type"] == "done"
    req = fake_ollama.requests[0]
    assert req["model"] == "big:latest"
    assert req["messages"][0]["role"] == "system" and "figpi" in req["messages"][0]["content"]
    assert req["messages"][-1] == {"role": "user", "content": "hi"}
    assert {t["function"]["name"] for t in req["tools"]} >= {"system_status", "read_file", "run_command", "remember"}
    saved = json.loads((cfg.state_dir / "history.json").read_text())
    assert [m["role"] for m in saved] == ["user", "assistant"]
    # a new Agent picks the history back up
    assert Agent(cfg).messages == saved


def test_tool_round_trip(cfg, fake_ollama, tmp_path):
    (tmp_path / "note.txt").write_text("42 bananas")
    fake_ollama.script = [
        [{"name": "read_file", "arguments": {"path": str(tmp_path / "note.txt")}}],
        "The file says 42 bananas.",
    ]
    agent = Agent(cfg)
    out, events = collect(agent, "what's in note.txt?")
    assert out == "The file says 42 bananas."
    kinds = [e["type"] for e in events]
    assert kinds.index("tool_call") < kinds.index("tool_result") < kinds.index("text")
    second = fake_ollama.requests[1]["messages"]
    assert second[-2]["role"] == "assistant" and second[-2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert second[-1] == {"role": "tool", "tool_name": "read_file", "content": "42 bananas"}


def test_unknown_tool_and_string_arguments(cfg, fake_ollama):
    fake_ollama.script = [[{"name": "launch_rockets", "arguments": '{"count": 3}'}], "ok"]
    agent = Agent(cfg)
    _, events = collect(agent, "go")
    res = next(e for e in events if e["type"] == "tool_result")
    assert "unknown tool" in res["result"]
    assert next(e for e in events if e["type"] == "tool_call")["arguments"] == {"count": 3}


def test_tool_round_limit(cfg, fake_ollama):
    cfg.agent.max_tool_rounds = 2
    fake_ollama.script = [[{"name": "system_status", "arguments": {}}]] * 5
    agent = Agent(cfg)
    collect(agent, "loop forever")
    assert len(fake_ollama.requests) == 3  # initial + 2 tool rounds, then stop


def test_ollama_down_reports_error(cfg):
    cfg.ollama.url = "http://127.0.0.1:9"  # nothing listens here
    agent = Agent(cfg)
    out, events = collect(agent, "hi")
    assert out == ""
    assert events[0]["type"] == "error" and "cannot reach Ollama" in events[0]["message"]
    assert agent.last_error


def test_history_window_keeps_tool_exchanges_whole(cfg, fake_ollama):
    cfg.agent.history_messages = 3
    agent = Agent(cfg)
    agent.messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_name": "x", "content": "r"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ]
    assert agent._window() == agent.messages[4:]


def test_think_only_sent_to_thinking_models(cfg, fake_ollama):
    cfg.ollama.think = False
    fake_ollama.script = ["a", "b"]
    agent = Agent(cfg)
    collect(agent, "x")
    assert "think" not in fake_ollama.requests[0]
    fake_ollama.capabilities["big:latest"].append("thinking")
    agent = Agent(cfg)
    collect(agent, "y")
    assert fake_ollama.requests[1]["think"] is False
