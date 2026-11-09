import json

from figpi.config import Config
from figpi.tools import build_tools, http_get, memory_text


def tools_for(tmp_path, groups):
    cfg = Config()
    cfg.state_dir = tmp_path / "state"
    cfg.tools.file_roots = [str(tmp_path / "ok")]
    cfg.tools.enabled = groups
    return {t.name: t for t in build_tools(cfg)}, cfg


def test_groups_filter(tmp_path):
    t, _ = tools_for(tmp_path, ["system"])
    assert list(t) == ["system_status"]
    t, _ = tools_for(tmp_path, ["gpio", "shell", "files", "http", "memory", "system"])
    assert len(t) == 9
    for tool in t.values():
        spec = tool.spec()
        assert spec["type"] == "function" and spec["function"]["parameters"]["type"] == "object"


def test_files_are_confined(tmp_path):
    t, _ = tools_for(tmp_path, ["files"])
    (tmp_path / "ok").mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    assert "outside the allowed roots" in t["read_file"].run({"path": str(tmp_path / "secret.txt")})
    assert "outside the allowed roots" in t["read_file"].run({"path": str(tmp_path / "ok" / ".." / "secret.txt")})
    assert t["write_file"].run({"path": str(tmp_path / "ok/sub/a.txt"), "content": "hi"}).startswith("wrote 2")
    assert t["write_file"].run({"path": str(tmp_path / "ok/sub/a.txt"), "content": "!", "append": True})
    assert t["read_file"].run({"path": str(tmp_path / "ok/sub/a.txt")}) == "hi!"
    assert "a.txt" in t["list_dir"].run({"path": str(tmp_path / "ok/sub")})


def test_shell_and_system(tmp_path):
    t, _ = tools_for(tmp_path, ["shell", "system"])
    out = t["run_command"].run({"command": "echo hello; echo oops >&2; exit 3"})
    assert out.startswith("exit code: 3") and "hello" in out and "[stderr]" in out and "oops" in out
    assert "timed out" in t["run_command"].run({"command": "sleep 5", "timeout": 1})
    info = json.loads(t["system_status"].run({}))
    assert "hostname" in info and "disk_free_gb" in info


def test_memory(tmp_path):
    t, cfg = tools_for(tmp_path, ["memory"])
    assert t["remember"].run({"note": "the user likes tea"}) == "saved"
    assert t["remember"].run({"note": "GPIO17 drives the fan"}) == "saved"
    assert "tea" in memory_text(cfg) and "fan" in memory_text(cfg)
    assert t["forget"].run({"contains": "tea"}) == "removed 1 notes"
    assert "tea" not in memory_text(cfg)


def test_gpio_validation(tmp_path):
    t, _ = tools_for(tmp_path, ["gpio"])
    out = t["gpio"].run({"action": "write", "pin": 99, "value": "high"})
    assert "error" in out  # either no pinctrl here, or pin range rejected


def test_http_get_rejects_bad_scheme():
    assert http_get({"url": "ftp://x"}).startswith("error")
