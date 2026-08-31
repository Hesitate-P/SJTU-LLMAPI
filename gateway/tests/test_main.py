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

MOCK_HANDLER = lambda request: httpx.Response(
    200, json={"id": "x", "choices": [{"message": {"content": "hi"}}]})


def make_client(monkeypatch, gateway_key=None, handler=MOCK_HANDLER):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    cfg = load_config_from_string()
    service = GatewayService(
        cfg, Breaker(), Stats(), {},
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        resolver=lambda host: ["93.184.216.34"])
    if gateway_key is not None:
        monkeypatch.setenv("GATEWAY_API_KEY", gateway_key)
    else:
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    return TestClient(create_app(cfg=cfg, service=service))


def load_config_from_string():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(CONFIG_YAML)
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


def test_models_fallback_to_static(monkeypatch):
    def failing(request):
        raise httpx.ConnectError("down")
    client = make_client(monkeypatch, handler=failing)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "m1"


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
