"""FR12/FR13 响应归一：normalize_response 纯函数 + StreamNormalizer 流式状态机 + forward 集成。"""
import json

import httpx

from app.config import AppConfig, ProviderConfig
from app.failover import Breaker
from app.forward import GatewayService
from app.normalize import StreamNormalizer, normalize_response
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


# ---------- StreamNormalizer 纯状态机（FR13，不经网络）----------

def chunk_line(delta: dict, model: str = "up-x", choices=None, **extra) -> str:
    """构造一条 chat.completion.chunk 形状的 SSE data: 行（不含行尾换行）。"""
    event = {"id": "c1", "object": "chat.completion.chunk", "model": model, **extra}
    event["choices"] = choices if choices is not None else [
        {"index": 0, "delta": delta, "finish_reason": None}]
    return "data: " + json.dumps(event, ensure_ascii=False)


def parse_data(line: str) -> dict:
    assert line.startswith("data: "), line
    return json.loads(line[len("data: "):])


def test_stream_full_think_single_event():
    n = StreamNormalizer("m1")
    out = n.feed(chunk_line({"content": "<think>先思考</think>正文"}))
    j = parse_data(out)
    assert j["model"] == "m1"  # model 回写
    d = j["choices"][0]["delta"]
    assert d["reasoning_content"] == "先思考"  # think 段改投
    assert d["content"] == "正文"
    assert n.finish() == []


def test_stream_empty_think_block():
    n = StreamNormalizer("m1")
    out = n.feed(chunk_line({"content": "<think></think>hello"}))
    d = parse_data(out)["choices"][0]["delta"]
    assert d["content"] == "hello"
    assert "reasoning_content" not in d  # 空思考段不产生字段


def test_stream_split_close_tag_short_tail_all_held():
    """`</th` + `ink>` 跨两个 data 事件分裂：短尾全部滞留，闭合后无丢失无重复。"""
    n = StreamNormalizer("m1")
    d1 = parse_data(n.feed(chunk_line({"content": "<think>abc</th"})))["choices"][0]["delta"]
    assert d1["content"] == ""  # "abc</th" ≤7 字符：全部滞留
    assert "reasoning_content" not in d1
    d2 = parse_data(n.feed(chunk_line({"content": "ink>def"})))["choices"][0]["delta"]
    assert d2["reasoning_content"] == "abc"
    assert d2["content"] == "def"
    assert n.finish() == []


def test_stream_split_close_tag_long_prefix_emits_safe_prefix():
    """长增量：输出安全前缀、滞留尾部 ≤7 字符，拼接后内容完整。"""
    n = StreamNormalizer("m1")
    d1 = parse_data(n.feed(chunk_line({"content": "<think>0123456789</th"})))["choices"][0]["delta"]
    assert d1["reasoning_content"] == "0123456"  # 前 7 字符安全前缀
    assert d1["content"] == ""
    d2 = parse_data(n.feed(chunk_line({"content": "ink>xyz"})))["choices"][0]["delta"]
    assert d2["reasoning_content"] == "789"
    assert d2["content"] == "xyz"
    # 拼接无丢失无重复
    assert d1["reasoning_content"] + d2["reasoning_content"] == "0123456789"


def test_stream_no_think_only_model_rewrite():
    n = StreamNormalizer("m1")
    out = n.feed(chunk_line({"content": "你好"}))
    j = parse_data(out)
    assert j["model"] == "m1"
    assert j["choices"][0]["delta"] == {"content": "你好"}  # content 直通不改形状
    out2 = n.feed(chunk_line({"content": "世界"}))
    assert parse_data(out2)["choices"][0]["delta"] == {"content": "世界"}


def test_stream_after_close_content_flows_normally():
    n = StreamNormalizer("m1")
    n.feed(chunk_line({"content": "<think>t</think>A"}))
    d = parse_data(n.feed(chunk_line({"content": "B"})))["choices"][0]["delta"]
    assert d == {"content": "B"}  # 闭合后的增量走 content
    assert "reasoning_content" not in d


def test_stream_role_delta_and_empty_content_do_not_decide_think():
    n = StreamNormalizer("m1")
    out = n.feed(chunk_line({"role": "assistant", "content": ""}))
    assert parse_data(out)["model"] == "m1"  # 仅 model 回写
    # 空content不消耗 think 起始判定：随后的 <think> 增量仍会切 THINK
    d = parse_data(n.feed(chunk_line({"content": "<think>x</think>y"})))["choices"][0]["delta"]
    assert d["reasoning_content"] == "x" and d["content"] == "y"


def test_stream_usage_line_only_model_rewrite():
    n = StreamNormalizer("m1")
    out = n.feed(chunk_line({}, choices=[], usage={"prompt_tokens": 3, "total_tokens": 5}))
    j = parse_data(out)
    assert j["model"] == "m1"  # 仅 model 回写
    assert j["usage"] == {"prompt_tokens": 3, "total_tokens": 5}
    assert j["choices"] == []
    # usage/[DONE] 行不参与 think 起始判定
    d = parse_data(n.feed(chunk_line({"content": "<think>x</think>y"})))["choices"][0]["delta"]
    assert d["reasoning_content"] == "x" and d["content"] == "y"


def test_stream_bad_json_line_passthrough():
    n = StreamNormalizer("m1")
    line = "data: {oops"
    assert n.feed(line) == line
    assert n.degraded == 1
    # 降级不污染后续行
    assert parse_data(n.feed(chunk_line({"content": "ok"})))["model"] == "m1"


def test_stream_done_and_non_data_lines_passthrough():
    n = StreamNormalizer("m1")
    assert n.feed("data: [DONE]") == "data: [DONE]"
    assert n.feed(": keep-alive") == ": keep-alive"
    assert n.feed("") == ""
    assert n.feed("data: ") == "data: "  # 空载荷：合法 SSE 直通，不计降级
    assert n.degraded == 0


def test_stream_finish_flushes_think_tail():
    """流在 THINK 态结束：滞留尾按 reasoning_content 增量补发，不丢数据。"""
    n = StreamNormalizer("m1")
    d = parse_data(n.feed(chunk_line({"content": "<think>abc"})))["choices"][0]["delta"]
    assert d["content"] == "" and "reasoning_content" not in d  # abc 全滞留
    lines = n.finish()
    assert len(lines) == 1
    j = parse_data(lines[0])
    assert j["model"] == "m1"
    assert j["choices"][0]["delta"]["reasoning_content"] == "abc"


def test_stream_finish_empty_when_nothing_pending():
    n = StreamNormalizer("m1")
    n.feed(chunk_line({"content": "plain"}))
    assert n.finish() == []


def test_stream_feed_bytes_assembles_partial_lines():
    """一个 bytes 块含两行半：完整行立即归一输出，半行滞留待后续块。"""
    n = StreamNormalizer("m1")
    chunk = (b'data: {"id":"c1","model":"up","choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
             b'data: {"id":"c1","model":"up","choices":[{"index":0,"delta":{"content":"b"}}]}\n\n'
             b'data: {"id":"c1","model":"up","choi')
    out = n.feed_bytes(chunk)
    data_lines = [l for l in out.split(b"\n\n") if l.startswith(b"data:")]
    assert len(data_lines) == 2  # 第三行半滞留
    assert all(json.loads(l[len("data: "):])["model"] == "m1" for l in data_lines)

    out2 = n.feed_bytes(b'ces":[{"index":0,"delta":{"content":"c"}}]}\n\n')
    data2 = [l for l in out2.split(b"\n\n") if l.startswith(b"data:")]
    assert len(data2) == 1
    j = json.loads(data2[0][len("data: "):])
    assert j["model"] == "m1"
    assert j["choices"][0]["delta"]["content"] == "c"


def test_stream_finish_bytes_flushes_unterminated_line():
    """流尾不完整行（完整 JSON 但无换行）：finish_bytes 处理后补 \\n。"""
    n = StreamNormalizer("m1")
    first = n.feed_bytes(b'data: {"model":"up","choices":[{"delta":{"content":"a"}}]}\n')
    assert json.loads(first[len("data: "):])["model"] == "m1"
    assert n.feed_bytes(b'data: {"model":"up","choices":[{"delta":{"content":"con"}}]}') == b""
    tail = n.finish_bytes()
    assert tail.endswith(b"\n")  # 补 \n
    j = json.loads(tail[:-1][len("data: "):])
    assert j["model"] == "m1"  # 残余行也被归一
    assert j["choices"][0]["delta"]["content"] == "con"


def test_stream_finish_bytes_degrades_truncated_json_line():
    """流尾被截断的坏 JSON 行：原样透出（补 \\n），计降级不抛异常。"""
    n = StreamNormalizer("m1")
    n.feed_bytes(b'data: {"model":"up","choices":[{"delta":{"con')
    tail = n.finish_bytes()
    assert tail == b'data: {"model":"up","choices":[{"delta":{"con\n'
    assert n.degraded == 1


def test_stream_lone_surrogate_degrades_to_original_bytes():
    n = StreamNormalizer("m1")
    # "\\ud800" 转义能被 json.loads 解析，但改写后重序列化 encode 抛
    # UnicodeEncodeError → 整行原样降级，绝不抛异常
    raw = b'data: {"model":"up","choices":[{"delta":{"content":"\\ud800ok"}}]}'
    assert n.feed_bytes(raw + b"\n") == raw + b"\n"
    assert n.degraded == 1


# ---------- forward 流式集成（FR13）----------

async def test_forward_stream_normalizes_think_and_model(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    up = "minimax-upstream-name"

    def line(content: str) -> bytes:
        return chunk_line({"content": content}, model=up).encode() + b"\n\n"

    async def gen():
        yield line("")  # 空 content：不参与判定，仅 model 回写
        yield line("<think>Let me ")
        yield line("think</th")  # </think> 跨事件分裂
        yield line("ink>Answer")
        yield line(" here")
        yield ("data: " + json.dumps(
            {"id": "c1", "model": up, "choices": [],
             "usage": {"total_tokens": 9}}, ensure_ascii=False)).encode() + b"\n\n"
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=gen())

    result = await minimax_service(handler).chat(
        {"model": "minimax", "messages": [{"role": "user", "content": "hi"}],
         "stream": True})
    assert result.status_code == 200
    assert result.provider == "sjtu"
    body = b"".join([c async for c in result.stream])

    events = []
    done_seen = False
    for raw in body.split(b"\n\n"):
        if not raw.startswith(b"data: "):
            continue
        payload = raw[len("data: "):]
        if payload == b"[DONE]":
            done_seen = True
            continue
        events.append(json.loads(payload))
    assert done_seen  # [DONE] 保留
    assert b"<think>" not in body and b"</think>" not in body  # 标签不外泄
    assert all(e["model"] == "minimax" for e in events)  # 每个事件 model 回写
    deltas = [e["choices"][0]["delta"] for e in events if e["choices"]]
    assert "".join(d.get("reasoning_content", "") for d in deltas) == "Let me think"
    assert "".join(d.get("content", "") for d in deltas) == "Answer here"
    assert events[-1]["usage"] == {"total_tokens": 9}  # usage 往返保真
