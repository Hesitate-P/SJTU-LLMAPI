"""v2 聚合层端到端集成测试：上下文路由（413/头注入）、流式归一、models 聚合。

模式同 test_integration.py：MockTransport 模拟上游 + resolver 注入公网 IP，
只测网关行为，不发真实网络请求。
"""
import json

import httpx
from fastapi.testclient import TestClient

from app.config import load_config
from app.failover import Breaker
from app.forward import GatewayService
from app.main import create_app
from app.stats import Stats

PUBLIC = lambda host: ["93.184.216.34"]

# sjtu 不配窗口（deepseek-chat/minimax 走内置表 262144/196608）；
# deepseek 显式 context_window 131072（yaml 语义）
CONFIG_YAML = (
    "providers:\n"
    "  - name: sjtu\n    base_url: https://sjtu.test/v1\n"
    "    api_key_env: SJTU_API_KEY\n    models: [deepseek-chat, minimax]\n    priority: 1\n"
    "  - name: deepseek\n    base_url: https://deepseek.test/v1\n"
    "    api_key_env: DEEPSEEK_API_KEY\n    models: [deepseek-chat]\n    priority: 2\n"
    "    context_window: 131072\n"
)


def build(tmp_path, handler, monkeypatch, env=None):
    for k, v in (env or {"SJTU_API_KEY": "s", "DEEPSEEK_API_KEY": "d"}).items():
        monkeypatch.setenv(k, v)
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    cfg = load_config(str(path), resolver=PUBLIC)
    service = GatewayService(cfg, Breaker(), Stats(), {},
                             client_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(handler)),
                             resolver=PUBLIC)
    return TestClient(create_app(cfg=cfg, service=service))


def test_overlength_routes_to_capable(tmp_path, monkeypatch):
    """520K 字符 ≈143K tokens：> deepseek 131072 被剔除，< sjtu 262144 由 sjtu 服务，
    响应头披露服务者窗口，且 deepseek 上游零调用。"""
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"id": "x", "model": "deepseek-chat",
                                         "choices": [{"message": {"content": "ok"}}]})

    client = build(tmp_path, handler, monkeypatch)
    resp = client.post("/v1/chat/completions", json={
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "a" * 520000}]})
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "sjtu"
    assert resp.headers["x-gateway-context-limit"] == "262144"
    assert calls == ["sjtu.test"]  # deepseek.test 未被调用


def test_all_too_large_local_413(tmp_path, monkeypatch):
    """1.2M 字符 ≈330K tokens 超过全部候选窗口 → 本地 413，零上游调用。"""
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"id": "x"})  # 不应到达

    client = build(tmp_path, handler, monkeypatch)
    resp = client.post("/v1/chat/completions", json={
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": "a" * 1200000}]})
    assert resp.status_code == 413
    err = resp.json()["error"]
    assert err["code"] == "context_length_exceeded"
    assert err["type"] == "invalid_request_error"
    assert calls == []


def test_stream_normalization_end_to_end(tmp_path, monkeypatch):
    """minimax 上游分块 SSE（含 <think> 前缀，闭合标签跨 data 事件、行跨 chunk）：
    客户端逐行读到的每个 data 行 model 均为请求名，思考段累计进
    reasoning_content，闭合后 content 干净无标签，[DONE] 保留在尾。"""
    upstream_model = "minimax-m2.7-internal"

    def sse(content, role=None):
        delta = {}
        if role is not None:
            delta["role"] = role
        delta["content"] = content
        return ("data: " + json.dumps({
            "model": upstream_model,
            "choices": [{"index": 0, "delta": delta}]})).encode()

    full = b"".join([
        sse(role="assistant", content=""), b"\n\n",
        sse(content="<think>思"), b"\n\n",
        sse(content="考"), b"\n\n",
        sse(content="</thi"), b"\n\n",   # 闭合标签前半
        sse(content="nk>正文"), b"\n\n",  # 闭合标签后半 + 干净正文
        sse(content="！"), b"\n\n",
        b"data: [DONE]\n\n",
    ])
    # 字节切块：前块截在第二行行中（行缓冲组装），think 中段再切一刀
    cut1 = full.index(b"<think>") + 3
    cut2 = full.index(b"</thi") + 2
    chunks = [full[:cut1], full[cut1:cut2], full[cut2:]]

    async def gen():
        for c in chunks:
            yield c

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=gen())

    client = build(tmp_path, handler, monkeypatch)
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "minimax", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]}) as resp:
        assert resp.status_code == 200
        assert resp.headers["x-gateway-provider"] == "sjtu"
        assert resp.headers["x-gateway-context-limit"] == "196608"
        lines = [ln for ln in resp.iter_lines() if ln.startswith("data:")]
        body = "\n".join(lines)

    assert lines[-1] == "data: [DONE]"  # 结束标记保留在尾
    assert "<think>" not in body and "</think>" not in body  # 标签不外泄
    events = [json.loads(ln[len("data:"):]) for ln in lines[:-1]]
    assert events  # 至少有事件
    assert all(ev["model"] == "minimax" for ev in events)  # 每行 model 回写请求名
    reasoning = "".join(
        c["delta"].get("reasoning_content", "")
        for ev in events for c in ev.get("choices", []))
    content = "".join(
        c["delta"].get("content", "")
        for ev in events for c in ev.get("choices", []))
    assert reasoning == "思考"  # 思考段全部改投 reasoning_content
    assert content == "正文！"  # 闭合后正文干净、无残留标签


def test_models_aggregated(tmp_path, monkeypatch):
    """/v1/models 返回配置目录并集（含优先级最高的供应商窗口），不打上游。"""
    def handler(request):
        raise AssertionError("聚合语义下 /v1/models 不应请求任何上游")

    client = build(tmp_path, handler, monkeypatch)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "config"
    data = resp.json()["data"]
    assert [m["id"] for m in data] == ["deepseek-chat", "minimax"]
    by_id = {m["id"]: m for m in data}
    assert by_id["deepseek-chat"]["context_window"] == 262144  # 最优先 sjtu 的内置窗口
    assert by_id["minimax"]["context_window"] == 196608
    assert all(m["object"] == "model" and m["owned_by"] == "gateway" for m in data)
