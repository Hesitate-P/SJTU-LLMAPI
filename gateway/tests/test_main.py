import json
import tempfile

import httpx
from fastapi.testclient import TestClient

from app.config import load_config
from app.failover import Breaker
from app.forward import GatewayService
from app.main import create_app
from app.stats import Stats

PUBLIC = ["93.184.216.34"]
RES = lambda host: PUBLIC

CONFIG_YAML = """
providers:
  - name: sjtu
    base_url: https://sjtu.test/v1
    api_key_env: SJTU_API_KEY
    models: [m1]
    priority: 1
"""

# Task 6 聚合语义用：sjtu 含 model_map 键；deepseek 显式窗口但环境变量缺失
# （不可用）——目录聚合不论 available 仍应列出其模型
AGG_CONFIG_YAML = """
providers:
  - name: sjtu
    base_url: https://sjtu.test/v1
    api_key_env: SJTU_API_KEY
    models: [deepseek-chat]
    model_map: {minimax: minimax-m2.7}
    priority: 1
  - name: deepseek
    base_url: https://deepseek.test/v1
    api_key_env: DEEPSEEK_API_KEY
    models: [deepseek-chat]
    priority: 2
    context_window: 131072
"""

MOCK_HANDLER = lambda request: httpx.Response(
    200, json={"id": "x", "choices": [{"message": {"content": "hi"}}]})


def make_client(monkeypatch, gateway_key=None, handler=MOCK_HANDLER,
                yaml_text=CONFIG_YAML):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    cfg = load_config_from_string(yaml_text)
    service = GatewayService(
        cfg, Breaker(), Stats(), {},
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=lambda host: ["93.184.216.34"])
    if gateway_key is not None:
        monkeypatch.setenv("GATEWAY_API_KEY", gateway_key)
    else:
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    return TestClient(create_app(cfg=cfg, service=service))


def load_config_from_string(yaml_text=CONFIG_YAML):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as fh:
        fh.write(yaml_text)
        path = fh.name
    return load_config(path, environ={"SJTU_API_KEY": "s"}, resolver=RES)


def test_chat_completions_happy_path(monkeypatch):
    client = make_client(monkeypatch)
    resp = client.post("/v1/chat/completions", json={"model": "m1", "messages": []})
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "sjtu"
    assert resp.json()["id"] == "x"


def test_auth_required_when_key_set(monkeypatch):
    client = make_client(monkeypatch, gateway_key="secret")
    assert client.post("/v1/chat/completions", json={"model": "m1"}).status_code == 401
    ok = client.post("/v1/chat/completions", json={"model": "m1"},
                     headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_unknown_model_404(monkeypatch):
    client = make_client(monkeypatch)
    resp = client.post("/v1/chat/completions", json={"model": "nope"})
    assert resp.status_code == 404


def test_models_aggregated_from_config(monkeypatch):
    """v2 聚合语义：不再打上游 /models，直接返回配置目录并集
    （含 model_map 客户端可见键；不论 provider available），
    context_window 取最优先可服务供应商的窗口解析值。"""
    def failing(request):
        raise AssertionError("聚合语义下 /v1/models 不应请求任何上游")
    client = make_client(monkeypatch, handler=failing, yaml_text=AGG_CONFIG_YAML)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "config"
    data = resp.json()["data"]
    assert [m["id"] for m in data] == ["deepseek-chat", "minimax"]  # 并集排序
    assert all(m["object"] == "model" and m["owned_by"] == "gateway" for m in data)
    by_id = {m["id"]: m for m in data}
    # deepseek-chat 由最优先的 sjtu（priority 1，内置 262144）解析窗口，
    # 而非不可用的 deepseek 的显式 131072；minimax 走内置 196608
    assert by_id["deepseek-chat"]["context_window"] == 262144
    assert by_id["minimax"]["context_window"] == 196608


def test_chat_success_carries_context_limit_header(monkeypatch):
    client = make_client(monkeypatch, yaml_text=AGG_CONFIG_YAML)
    resp = client.post("/v1/chat/completions", json={"model": "deepseek-chat"})
    assert resp.status_code == 200
    assert resp.headers["x-gateway-context-limit"] == "262144"  # 服务者 sjtu 的内置窗口


def test_stream_chat_carries_context_limit_header(monkeypatch):
    async def gen():
        yield b"data: {\"model\":\"upstream\",\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=gen())

    client = make_client(monkeypatch, handler=handler, yaml_text=AGG_CONFIG_YAML)
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "minimax", "stream": True}) as resp:
        assert resp.status_code == 200
        assert resp.headers["x-gateway-context-limit"] == "196608"  # 流式同样注入
        lines = [ln for ln in resp.iter_lines() if ln.startswith("data:")]
    assert json.loads(lines[0][len("data:"):])["model"] == "minimax"  # 顺带钉住流式 model 回写


def test_stats_endpoint(monkeypatch):
    client = make_client(monkeypatch)
    client.post("/v1/chat/completions", json={"model": "m1"})
    snap = client.get("/stats").json()
    assert snap["sjtu"]["success"] == 1


def test_health_accessible_without_key(monkeypatch):
    client = make_client(monkeypatch, gateway_key="secret")
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_stats_still_requires_key(monkeypatch):
    client = make_client(monkeypatch, gateway_key="secret")
    assert client.get("/stats").status_code == 401
    ok = client.get("/stats", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_health_endpoint(monkeypatch):
    client = make_client(monkeypatch)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["providers"]["sjtu"]["available"] is True
