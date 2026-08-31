from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app


def test_health_returns_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n  - name: sjtu\n    base_url: https://sjtu.test/v1\n"
        "    api_key_env: SJTU_API_KEY\n    models: [m1]\n    priority: 1\n",
        encoding="utf-8")
    cfg = load_config(str(path), resolver=lambda host: ["93.184.216.34"])
    resp = TestClient(create_app(cfg=cfg)).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
