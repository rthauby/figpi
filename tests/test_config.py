from figpi.config import load


def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    cfg = load()
    assert cfg.ollama.url == "auto" and cfg.web.port == 8080 and cfg.source is None


def test_load_file(tmp_path):
    p = tmp_path / "figpi.toml"
    p.write_text('[ollama]\nurl = "http://10.0.0.5:11434"\nmodel = "qwen3:8b"\nthink = false\n'
                 '[web]\nport = 80\npassword = "pw"\n[tools]\nenabled = ["system", "gpio"]\n'
                 '[agent]\nname = "kitchen-pi"\n[wifi]\nssid = "net"\npassword = "x"\ncountry = "US"\n')
    cfg = load(p)
    assert cfg.ollama.url == "http://10.0.0.5:11434" and cfg.ollama.model == "qwen3:8b" and cfg.ollama.think is False
    assert cfg.web.port == 80 and cfg.web.password == "pw"
    assert cfg.tools.enabled == ["system", "gpio"]
    assert cfg.agent.name == "kitchen-pi" and cfg.wifi.country == "US"
    assert cfg.source == p


def test_env_overrides_auto(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.9:11434")
    cfg = load(tmp_path / "missing.toml")
    assert cfg.ollama.url == "http://192.168.1.9:11434"
