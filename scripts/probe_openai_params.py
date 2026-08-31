"""交大 LLM OpenAI 兼容参数支持度探针。

直连上游逐参数测试并分类：
  ✅ 支持（有行为证据）  🟡 接受（200，行为未证）  ❌ 拒绝（4xx）
凭据只从环境变量 SJTU_API_KEY 读取。节流 ~7s/请求，429 自动等待 35s 重试（最多 2 次）。
用法：set -a; . ./.env; set +a; cd gateway && uv run python ../scripts/probe_openai_params.py
"""
from __future__ import annotations

import json
import os
import time

import httpx

BASE = os.environ.get("SJTU_BASE", "https://models.sjtu.edu.cn/api/v1")
KEY = os.environ.get("SJTU_API_KEY", "")
MODEL = os.environ.get("PROBE_MODEL", "deepseek-chat")
GAP = float(os.environ.get("PROBE_GAP", "7"))
client = httpx.Client(
    headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
    timeout=httpx.Timeout(30, read=180),
)
results: list[dict] = []


def call(body: dict, label: str, check=None) -> None:
    """check: fn(resp_json)->(bool, note) 行为验证。"""
    for attempt in range(3):
        t0 = time.perf_counter()
        try:
            r = client.post(f"{BASE}/chat/completions", json=body)
        except httpx.HTTPError as e:
            results.append({"p": label, "v": "❌", "note": f"网络错误 {type(e).__name__}"})
            print(f"  ❌ {label}: 网络错误 {type(e).__name__}")
            break
        ms = (time.perf_counter() - t0) * 1000
        if r.status_code == 429 and attempt < 2:
            print(f"  … {label}: 429，等 35s 重试")
            time.sleep(35)
            continue
        if r.status_code == 200:
            j = r.json()
            note = f"{ms:.0f}ms"
            verdict = "🟡"
            if check:
                ok, why = check(j)
                verdict = "✅" if ok else "🟡"
                note = f"{ms:.0f}ms | {why}"
            results.append({"p": label, "v": verdict, "note": note})
            print(f"  {verdict} {label}: {note}")
        else:
            try:
                msg = r.json().get("error", {}).get("message", r.text[:120])
            except Exception:
                msg = r.text[:120]
            results.append({"p": label, "v": "❌", "note": f"HTTP {r.status_code}: {msg}"})
            print(f"  ❌ {label}: HTTP {r.status_code}: {msg}")
        break
    time.sleep(GAP)


def has_choices(j):
    return (bool(j.get("choices")), f"choices={len(j.get('choices', []))}")


def main() -> None:
    if not KEY:
        raise SystemExit("需要环境变量 SJTU_API_KEY")
    base_msg = [{"role": "user", "content": "从1数到5，空格分隔。"}]

    print(f"== 基线（无附加参数, model={MODEL}） ==")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 60}, "baseline", has_choices)

    print("== 采样参数（接受性 + 可证行为） ==")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 60, "temperature": 0.1}, "temperature")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 60, "top_p": 0.9}, "top_p")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 60, "top_k": 20}, "top_k(非OpenAI扩展)")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 60, "presence_penalty": 1.0}, "presence_penalty")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 60, "frequency_penalty": 1.0}, "frequency_penalty")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 60, "seed": 42}, "seed")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 60, "user": "probe-user-1"}, "user")

    def check_stop(j):
        c = j["choices"][0]
        content = c["message"]["content"] or ""
        return ("3" not in content or c.get("finish_reason") == "stop",
                f"finish={c.get('finish_reason')} content={content[:40]!r}")
    call({"model": MODEL, "messages": [{"role": "user", "content": "从1数到10，逗号分隔"}], "max_tokens": 100,
          "stop": ["4"]}, "stop", check_stop)

    def check_n(j):
        return (len(j.get("choices", [])) == 2, f"choices={len(j.get('choices', []))}")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 40, "n": 2}, "n=2", check_n)

    def check_mt(j):
        c = j["choices"][0]
        return (c.get("finish_reason") == "length" and (j.get("usage", {}).get("completion_tokens", 99) <= 12),
                f"finish={c.get('finish_reason')} completion={j.get('usage', {}).get('completion_tokens')}")
    call({"model": MODEL, "messages": [{"role": "user", "content": "写一篇200字短文"}], "max_tokens": 10},
         "max_tokens 截断", check_mt)
    call({"model": MODEL, "messages": [{"role": "user", "content": "写一篇200字短文"}],
          "max_completion_tokens": 10}, "max_completion_tokens", check_mt)

    def check_logprobs(j):
        lp = j["choices"][0].get("logprobs")
        return (bool(lp and lp.get("content")), f"logprobs keys={list((lp or {}).keys())}")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 20, "logprobs": True, "top_logprobs": 3},
         "logprobs+top_logprobs", check_logprobs)

    call({"model": MODEL, "messages": base_msg, "max_tokens": 30,
          "logit_bias": {"13": -100}}, "logit_bias")

    def check_json(j):
        content = j["choices"][0]["message"]["content"] or ""
        try:
            json.loads(content)
            return True, "输出为合法 JSON"
        except Exception:
            return False, f"非 JSON: {content[:40]!r}"
    call({"model": MODEL, "messages": [{"role": "user", "content": '返回一个 JSON 对象 {"ok": true}，不要其它内容'}],
          "max_tokens": 50, "response_format": {"type": "json_object"}}, "response_format=json_object", check_json)
    call({"model": MODEL, "messages": [{"role": "user", "content": "返回 {\"ok\": true}"}], "max_tokens": 50,
          "response_format": {"type": "json_schema", "json_schema": {
              "name": "ok", "schema": {"type": "object", "properties": {"ok": {"type": "boolean"}},
                                       "required": ["ok"], "additionalProperties": False}}}},
         "response_format=json_schema")

    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "查询城市天气",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]

    def check_tools(j):
        tc = j["choices"][0]["message"].get("tool_calls")
        return (bool(tc), f"tool_calls={tc and [(t['function']['name'], t['function']['arguments']) for t in tc]}")
    call({"model": MODEL, "tools": tools, "tool_choice": "auto",
          "messages": [{"role": "user", "content": "上海今天天气怎么样？必须用工具查"}], "max_tokens": 100},
         "tools+tool_choice=auto", check_tools)
    call({"model": MODEL, "tools": tools, "tool_choice": "none",
          "messages": [{"role": "user", "content": "上海天气？"}], "max_tokens": 60}, "tool_choice=none")
    call({"model": MODEL, "tools": tools,
          "messages": [{"role": "user", "content": "上海天气？"}], "max_tokens": 60,
          "parallel_tool_calls": False}, "parallel_tool_calls")

    legacy = {"functions": [{"name": "get_weather", "parameters": {"type": "object",
              "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
              "function_call": "auto", "model": MODEL, "max_tokens": 60,
              "messages": [{"role": "user", "content": "上海天气？"}]}
    call(legacy, "functions/function_call(旧版)")

    print("== 消息角色 / 内容形态 ==")

    def check_system(j):
        c = j["choices"][0]["message"]["content"] or ""
        return ("DATA" in c.upper(), f"system 生效回复[:40]={c[:40]!r}")
    call({"model": MODEL, "max_tokens": 40, "messages": [
        {"role": "system", "content": "无论用户说什么，只回复大写单词 DATA"},
        {"role": "user", "content": "你好"}]}, "system 角色", check_system)
    call({"model": MODEL, "max_tokens": 40, "messages": [
        {"role": "developer", "content": "只回复 DATA"}, {"role": "user", "content": "你好"}]}, "developer 角色")
    call({"model": MODEL, "max_tokens": 40, "messages": [
        {"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"},
        {"role": "user", "content": "从1数到3"}]}, "assistant 多轮")
    call({"model": MODEL, "tools": tools, "max_tokens": 80, "messages": [
        {"role": "user", "content": "上海天气？"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
          "function": {"name": "get_weather", "arguments": "{\"city\": \"上海\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "晴 28 度"},
        {"role": "user", "content": "用一句话总结"}]}, "tool 角色（回传结果）")

    # 1x1 红色 PNG
    png1px = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    img_body = {"model": os.environ.get("PROBE_VL_MODEL", "qwen"), "max_tokens": 50, "messages": [{
        "role": "user", "content": [
            {"type": "text", "text": "图里是什么颜色？一个词。"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png1px}"}}]}]}
    r = None
    for attempt in range(3):
        try:
            r = client.post(f"{BASE}/chat/completions", json=img_body)
        except httpx.HTTPError as e:
            print(f"  ❌ 图片输入(qwen): 网络错误 {type(e).__name__}")
            break
        if r.status_code == 429 and attempt < 2:
            time.sleep(35)
            continue
        break
    if r is not None:
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"] or ""
            results.append({"p": "图片输入(qwen, base64)", "v": "✅", "note": f"回复[:30]={content[:30]!r}"})
            print(f"  ✅ 图片输入(qwen): {content[:30]!r}")
        else:
            results.append({"p": "图片输入(qwen, base64)", "v": "❌", "note": f"HTTP {r.status_code}"})
            print(f"  ❌ 图片输入(qwen): HTTP {r.status_code} {r.text[:100]}")

    print("== 思考参数（reasoner/claw 系） ==")
    call({"model": "deepseek-reasoner", "max_tokens": 200, "messages": [
        {"role": "user", "content": "1+1=?"}]}, "reasoning_content 存在性",
        lambda j: (bool(j["choices"][0]["message"].get("reasoning_content")),
                   f"reasoning[:30]={(j['choices'][0]['message'].get('reasoning_content') or '')[:30]!r}"))
    call({"model": MODEL, "max_tokens": 40, "reasoning_effort": "low",
          "messages": base_msg}, "reasoning_effort")

    print("== OpenAI 专有参数（预期忽略或拒绝） ==")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 30, "service_tier": "default"}, "service_tier")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 30, "store": True}, "store")
    call({"model": MODEL, "messages": base_msg, "max_tokens": 30, "metadata": {"a": "1"}}, "metadata")

    print("\n######## 支持度汇总 ########")
    for r_ in results:
        print(f"  {r_['v']} {r_['p']:<28} {r_['note']}")
    ok = sum(1 for r_ in results if r_["v"] == "✅")
    mid = sum(1 for r_ in results if r_["v"] == "🟡")
    bad = sum(1 for r_ in results if r_["v"] == "❌")
    print(f"\n  合计: ✅ 行为实证 {ok} | 🟡 接受未证 {mid} | ❌ 拒绝 {bad}")


if __name__ == "__main__":
    main()
