"""FR12 非流式响应归一：normalize_response 纯函数 + forward 集成。"""
import json

import httpx

from app.config import AppConfig, ProviderConfig
from app.failover import Breaker
from app.forward import GatewayService
from app.normalize import normalize_response
from app.stats import Stats


# ---------- 纯函数 ----------

def test_model_rewrite():
    j = {"model": "upstream-m1", "choices": []}
    out = normalize_response(j, "m1")
    assert out["model"] == "m1"


def test_think_stripped_into_reasoning_content():
    j = {"model": "minimax", "choices": [
        {"index": 0, "message": {"role": "assistant",
                                 "content": "<think>先思考</think>正文回答"}}]}
    out = normalize_response(j, "client-model")
    assert out["model"] == "client-model"
    msg = out["choices"][0]["message"]
    assert msg["content"] == "正文回答"
    assert msg["reasoning_content"] == "先思考"


def test_think_remainder_lstripped():
    j = {"model": "x", "choices": [
        {"message": {"content": "<think>思路</think>\n\n  正文"}}]}
    out = normalize_response(j, "m1")
    assert out["choices"][0]["message"]["content"] == "正文"


def test_unclosed_think_left_untouched():
    j = {"model": "x", "choices": [
        {"message": {"content": "<think>只有开头没闭合"}}]}
    out = normalize_response(j, "m1")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "<think>只有开头没闭合"
    assert "reasoning_content" not in msg
    assert out["model"] == "m1"  # model 回写不受 message 未剥离去影响


def test_content_not_starting_with_think_untouched():
    j = {"model": "x", "choices": [
        {"message": {"content": "正文中间出现 </think> 标签"}}]}
    out = normalize_response(j, "m1")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "正文中间出现 </think> 标签"
    assert "reasoning_content" not in msg


def test_existing_reasoning_content_prepended():
    j = {"model": "x", "choices": [
        {"message": {"content": "<think>新思考</think>正文",
                     "reasoning_content": "原有思考"}}]}
    out = normalize_response(j, "m1")
    msg = out["choices"][0]["message"]
    assert msg["reasoning_content"] == "新思考原有思考"
    assert msg["content"] == "正文"


def test_non_str_content_untouched():
    j = {"model": "x", "choices": [
        {"message": {"role": "assistant", "content": None}},
        {"message": {"content": ["part", {"type": "text", "text": "hi"}]}}]}
    out = normalize_response(j, "m1")
    assert out["choices"][0]["message"]["content"] is None
    assert out["choices"][1]["message"]["content"] == ["part", {"type": "text", "text": "hi"}]


def test_missing_choices_returns_original():
    j = {"id": "x"}
    out = normalize_response(j, "m1")
    assert out is j
    assert "model" not in j  # 结构异常降级：连 model 回写也不做


def test_non_dict_returns_original():
    bad = ["not", "a", "dict"]
    assert normalize_response(bad, "m1") is bad


# ---------- forward 集成 ----------

def minimax_service(handler):
    """sjtu 映射 minimax -> 上游名；模拟 minimax 风格 think 前缀响应。"""
    cfg = AppConfig(providers=[
        ProviderConfig(name="sjtu", base_url="https://sjtu.test/v1",
                       api_key_env="SJTU_API_KEY", priority=1,
                       model_map={"minimax": "minimax-upstream-name"}),
    ])
    return GatewayService(cfg, Breaker(), Stats(), {},
                          client_factory=lambda: httpx.AsyncClient(
                              transport=httpx.MockTransport(handler)),
                          resolver=lambda host: ["93.184.216.34"])


async def test_forward_non_stream_normalizes_minimax_style(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "cmpl-1", "model": "minimax-upstream-name",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "<think>思考过程</think>最终答案"}}]})

    result = await minimax_service(handler).chat(
        {"model": "minimax", "messages": [{"role": "user", "content": "hi"}]})
    assert result.status_code == 200
    body = json.loads(result.body)
    assert body["model"] == "minimax"  # 回写客户端可见名
    msg = body["choices"][0]["message"]
    assert msg["content"] == "最终答案"
    assert msg["reasoning_content"] == "思考过程"


async def test_forward_multi_choice_think_and_usage_survive(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "id": "cmpl-2", "model": "minimax-upstream-name",
            "choices": [
                {"index": 0, "message": {"role": "assistant",
                                         "content": "<think>思路甲</think>答案甲"}},
                {"index": 1, "message": {"role": "assistant",
                                         "content": "<think>思路乙</think>答案乙"}},
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 7,
                      "total_tokens": 17}})

    result = await minimax_service(handler).chat(
        {"model": "minimax", "messages": [{"role": "user", "content": "hi"}]})
    assert result.status_code == 200
    body = json.loads(result.body)
    assert body["model"] == "minimax"
    # usage 等无关字段往返保真
    assert body["usage"] == {"prompt_tokens": 10, "completion_tokens": 7,
                             "total_tokens": 17}
    # 两个 choices 的 think 前缀都被剥离
    first, second = body["choices"]
    assert first["message"]["content"] == "答案甲"
    assert first["message"]["reasoning_content"] == "思路甲"
    assert second["message"]["content"] == "答案乙"
    assert second["message"]["reasoning_content"] == "思路乙"


async def test_forward_lone_surrogate_degrades_to_original_bytes(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    # JSON 转义 "\\ud800"：json.loads 能解析出未配对代理，但
    # ensure_ascii=False 的 dumps→encode 会抛 UnicodeEncodeError——
    # 归一必须降级返回原始 bytes，绝不把成功响应变成异常
    raw = b'{"model":"up","choices":[{"message":{"content":"\\ud800ok"}}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=raw,
                              headers={"content-type": "application/json"})

    result = await minimax_service(handler).chat(
        {"model": "minimax", "messages": [{"role": "user", "content": "hi"}]})
    assert result.status_code == 200
    assert result.body == raw  # 序列化失败降级：原样 bytes，未抛异常


async def test_forward_client_error_not_normalized(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"model": "minimax-upstream-name",
                                         "error": {"message": "bad request"}})

    result = await minimax_service(handler).chat(
        {"model": "minimax", "messages": [{"role": "user", "content": "hi"}]})
    assert result.status_code == 400
    assert json.loads(result.body)["model"] == "minimax-upstream-name"  # 错误原貌透传


async def test_forward_non_json_ok_body_passthrough(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all",
                              headers={"content-type": "text/plain"})

    result = await minimax_service(handler).chat(
        {"model": "minimax", "messages": [{"role": "user", "content": "hi"}]})
    assert result.status_code == 200
    assert result.body == b"not json at all"  # 解析失败降级原样 bytes


async def test_forward_no_choices_ok_body_passthrough(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    result = await minimax_service(handler).chat(
        {"model": "minimax", "messages": [{"role": "user", "content": "hi"}]})
    assert result.status_code == 200
    assert json.loads(result.body) == {"ok": True}  # 非 chat 形状：原样返回
