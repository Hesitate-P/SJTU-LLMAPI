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


def test_config_path_outside_allowed_roots_rejected():
    with pytest.raises(ValueError, match="越界"):
        load_config("/root/evil.yaml", environ={}, resolver=RES)


def test_config_path_inside_tmp_allowed(tmp_path):
    path = tmp_path / "ok.yaml"
    path.write_text(YAML, encoding="utf-8")
    cfg = load_config(str(path), environ={"SJTU_API_KEY": "k", "DEEPSEEK_API_KEY": "k"}, resolver=RES)
    assert cfg.providers


def test_context_resolution_priority(tmp_path):
    from app.config import context_for

    path = tmp_path / "windows.yaml"
    path.write_text(
        """
providers:
  - name: A
    base_url: https://a.test/v1
    api_key_env: K1
    models: [m1, m2]
    priority: 1
    context_window: 100000
    model_contexts:
      m1: 50000
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: K2
    models: [deepseek-chat, minimax]
    priority: 2
  - name: C
    base_url: https://c.test/v1
    api_key_env: K3
    models: [unknown-model]
    priority: 3
""",
        encoding="utf-8",
    )
    cfg = load_config(str(path), environ={"K1": "k", "K2": "k", "K3": "k"}, resolver=RES)
    a, sjtu, c = cfg.providers
    # model_contexts > context_window > 内置表 > 8192
    assert context_for(a, "m1") == 50000
    assert context_for(a, "m2") == 100000
    assert context_for(sjtu, "deepseek-chat") == 262144
    assert context_for(sjtu, "minimax") == 196608
    assert context_for(c, "unknown-model") == 8192


def test_monotonic_warning(tmp_path, caplog):
    import logging

    def load(text):
        path = tmp_path / "config.yaml"
        path.write_text(text, encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="gateway"):
            return load_config(str(path), environ={"SJTU_API_KEY": "k", "SMALL_KEY": "k"}, resolver=RES)

    def window_warnings():
        return [r for r in caplog.records if "窗口" in r.getMessage()]

    # sjtu(256K 内置) → small(配置 65536)，同服务 deepseek-chat：严格变小 → 告警
    load(
        """
providers:
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY
    models: [deepseek-chat]
    priority: 1
  - name: small
    base_url: https://small.test/v1
    api_key_env: SMALL_KEY
    models: [deepseek-chat]
    priority: 2
    context_window: 65536
"""
    )
    records = window_warnings()
    assert len(records) == 1
    text = records[0].getMessage()
    assert "deepseek-chat" in text
    assert "sjtu" in text and "small" in text
    assert "65536" in text and "262144" in text

    # 窗口单调（兜底更大）时无该 WARNING
    caplog.clear()
    load(
        """
providers:
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY
    models: [deepseek-chat]
    priority: 1
    context_window: 65536
  - name: small
    base_url: https://small.test/v1
    api_key_env: SMALL_KEY
    models: [deepseek-chat]
    priority: 2
"""
    )
    assert window_warnings() == []

    # 窗口相等同样合法（只警告"严格变小"）
    caplog.clear()
    load(
        """
providers:
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY
    models: [deepseek-chat]
    priority: 1
    context_window: 65536
  - name: small
    base_url: https://small.test/v1
    api_key_env: SMALL_KEY
    models: [deepseek-chat]
    priority: 2
    context_window: 65536
"""
    )
    assert window_warnings() == []
