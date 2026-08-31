import pytest

from app.config import load_config

PUBLIC = ["93.184.216.34"]
RES = lambda host: PUBLIC

YAML = """
listen_port: 9000
providers:
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models: [deepseek-chat]
    priority: 2
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY
    models: [deepseek-chat, minimax]
    priority: 1
    proactive_rate_limit:
      requests_per_minute: 9
      max_wait_seconds: 2
failover:
  cooldown_429_seconds: 30
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(YAML, encoding="utf-8")
    return load_config(str(path), environ={"SJTU_API_KEY": "k1", "DEEPSEEK_API_KEY": "k2"}, resolver=RES)


def test_providers_sorted_by_priority(cfg):
    assert [p.name for p in cfg.providers] == ["sjtu", "deepseek"]


def test_fields_parsed(cfg):
    sjtu = cfg.providers[0]
    assert sjtu.base_url == "https://models.sjtu.edu.cn/api/v1"
    assert sjtu.available and sjtu.unavailable_reason == ""
    assert sjtu.proactive_rate_limit.requests_per_minute == 9
    assert cfg.failover.cooldown_429_seconds == 30
    assert cfg.listen_port == 9000


def test_missing_key_env_marks_unavailable(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(YAML, encoding="utf-8")
    cfg = load_config(str(path), environ={"SJTU_API_KEY": "k1"}, resolver=RES)
    deepseek = next(p for p in cfg.providers if p.name == "deepseek")
    assert deepseek.available is False
    assert "DEEPSEEK_API_KEY" in deepseek.unavailable_reason


def test_forbidden_base_url_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n  - name: bad\n    base_url: http://10.0.0.5/v1\n"
        "    api_key_env: X\n    priority: 1\n    models: [m]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(str(path), environ={"X": "k"}, resolver=RES)


def test_no_providers_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("providers: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provider"):
        load_config(str(path), environ={}, resolver=RES)


def test_zero_rate_limit_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n  - name: p\n    base_url: https://a.test/v1\n"
        "    api_key_env: X\n    priority: 1\n    models: [m]\n"
        "    proactive_rate_limit:\n      requests_per_minute: 0\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requests_per_minute"):
        load_config(str(path), environ={"X": "k"}, resolver=RES)
