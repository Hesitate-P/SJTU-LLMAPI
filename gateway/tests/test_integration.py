import httpx
from fastapi.testclient import TestClient

from app.config import load_config
from app.failover import Breaker
from app.forward import GatewayService
from app.main import create_app
from app.stats import Stats

PUBLIC = lambda host: ["93.184.216.34"]


def build(tmp_path, handler, monkeypatch, env=None):
    for k, v in (env or {"SJTU_API_KEY": "s", "DEEPSEEK_API_KEY": "d"}).items():
        monkeypatch.setenv(k, v)
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n"
        "  - name: sjtu\n    base_url: https://sjtu.test/v1\n"
        "    api_key_env: SJTU_API_KEY\n    models: [m1]\n    priority: 1\n"
        "  - name: deepseek\n    base_url: https://deepseek.test/v1\n"
        "    api_key_env: DEEPSEEK_API_KEY\n    models: [m1]\n    priority: 2\n",
        encoding="utf-8")
    cfg = load_config(str(path), resolver=PUBLIC)
    service = GatewayService(cfg, Breaker(), Stats(), {},
                             client_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(handler)),
                             resolver=PUBLIC)
    return TestClient(create_app(cfg=cfg, service=service))


def test_rate_limit_scenario_switches_provider_and_records_stats(tmp_path, monkeypatch):
    state = {"sjtu_calls": 0}

    def handler(request):
        if request.url.host == "sjtu.test":
            state["sjtu_calls"] += 1
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"id": "y", "choices": []})

    client = build(tmp_path, handler, monkeypatch)
    for _ in range(3):
        resp = client.post("/v1/chat/completions", json={"model": "m1"})
        assert resp.status_code == 200
        assert resp.headers["x-gateway-provider"] == "deepseek"
    stats = client.get("/stats").json()
    assert stats["sjtu"]["rate_limited"] == 1          # 第二次起熔断直接跳过
    assert stats["deepseek"]["success"] == 3
    assert state["sjtu_calls"] == 1


def test_streaming_response_passthrough(tmp_path, monkeypatch):
    async def gen():
        yield "data: {\"delta\": \"你\"}\n\n".encode("utf-8")
        yield "data: {\"delta\": \"好\"}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    client = build(tmp_path, lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=gen()), monkeypatch)
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "m1", "stream": True}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_bytes())
    assert b"[DONE]" in body


def test_no_auth_needed_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    client = build(tmp_path, lambda r: httpx.Response(200, json={"id": "z"}), monkeypatch)
    assert client.post("/v1/chat/completions", json={"model": "m1"}).status_code == 200


def test_provider_with_missing_key_is_skipped(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"id": "z"})

    client = build(tmp_path, handler, monkeypatch,
                   env={"SJTU_API_KEY": "", "DEEPSEEK_API_KEY": "d"})
    resp = client.post("/v1/chat/completions", json={"model": "m1"})
    assert resp.headers["x-gateway-provider"] == "deepseek"
    assert calls == ["deepseek.test"]
