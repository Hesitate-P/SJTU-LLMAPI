"""结构化日志测试：缺密钥告警、切换轨迹、脱敏（绝不记密钥值）。"""
import logging

import httpx

from app.config import AppConfig, ProviderConfig, load_config
from app.failover import Breaker
from app.forward import GatewayService
from app.stats import Stats

PUBLIC = ["93.184.216.34"]
RES = lambda host: PUBLIC


def make_service(handler):
    cfg = AppConfig(providers=[
        ProviderConfig(name="sjtu", base_url="https://sjtu.test/v1", api_key_env="SJTU_API_KEY",
                       priority=1, models=["m1"]),
        ProviderConfig(name="deepseek", base_url="https://deepseek.test/v1",
                       api_key_env="DEEPSEEK_API_KEY", priority=2, models=["m1"]),
    ])
    return GatewayService(
        cfg, Breaker(), Stats(), {},
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=RES,
    )


def test_missing_key_env_warns(tmp_path, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n"
        "  - name: deepseek\n"
        "    base_url: https://api.deepseek.com/v1\n"
        "    api_key_env: DEEPSEEK_API_KEY\n"
        "    models: [deepseek-chat]\n"
        "    priority: 1\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="gateway"):
        cfg = load_config(str(path), environ={"SJTU_API_KEY": "k"}, resolver=RES)
    assert next(p for p in cfg.providers if p.name == "deepseek").available is False
    assert "DEEPSEEK_API_KEY 未设置" in caplog.text
    assert any(r.levelno == logging.WARNING and r.name == "gateway" for r in caplog.records)
    assert "k" not in [r.message for r in caplog.records if r.levelno == logging.WARNING]


async def test_chat_logs_switch_trace(monkeypatch, caplog):
    monkeypatch.setenv("SJTU_API_KEY", "sjtu-secret-value")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret-value")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sjtu.test":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"id": "x", "choices": []})

    with caplog.at_level(logging.INFO, logger="gateway"):
        result = await make_service(handler).chat({"model": "m1"})
    assert result.provider == "deepseek"
    assert result.status_code == 200

    info_lines = [r.getMessage() for r in caplog.records
                  if r.levelno == logging.INFO and r.name == "gateway"]
    assert any("model=m1" in line for line in info_lines)
    assert any("switch_trace" in line for line in info_lines)
    # sjtu 因 429 被切走，轨迹中应有 (sjtu, rate_limited)（%s 格式化后的 str 形式）
    assert any("('sjtu', 'rate_limited')" in line for line in info_lines)
    # 日志绝不包含任何密钥值
    assert "sjtu-secret-value" not in caplog.text
    assert "deepseek-secret-value" not in caplog.text


async def test_no_secrets_in_logs(monkeypatch, caplog):
    monkeypatch.setenv("SJTU_API_KEY", "super-secret-key-123")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    with caplog.at_level(logging.INFO, logger="gateway"):
        result = await make_service(handler).chat({"model": "m1"})
    assert result.status_code == 200
    assert "super-secret-key-123" not in caplog.text
