"""第二轮行为验证 + 隐藏模型探测（一次性）。"""
import json
import os
import time

import httpx

BASE = "https://models.sjtu.edu.cn/api/v1"
c = httpx.Client(headers={"Authorization": f"Bearer {os.environ['SJTU_API_KEY']}"}, timeout=120)


def post(body):
    r = c.post(f"{BASE}/chat/completions", json=body)
    time.sleep(7)
    return r


def one(label, body, check):
    r = post(body)
    if r.status_code != 200:
        print(f"  ❌ {label}: HTTP {r.status_code} {r.text[:80]}")
        return
    ok, note = check(r.json())
    print(f"  {'✅' if ok else '🟡'} {label}: {note}")


msg = [{"role": "user", "content": "用一个词形容春天。"}]

outs = []
for _ in range(2):
    r = post({"model": "deepseek-chat", "messages": msg, "max_tokens": 20, "temperature": 0})
    outs.append(r.json()["choices"][0]["message"]["content"])
print(f"  {'✅' if outs[0] == outs[1] else '🟡'} temperature=0 确定性: {outs}")

outs = []
for _ in range(2):
    r = post({"model": "deepseek-chat", "messages": msg, "max_tokens": 20, "temperature": 0.9, "seed": 123})
    outs.append(r.json()["choices"][0]["message"]["content"])
print(f"  {'✅' if outs[0] == outs[1] else '🟡'} seed=123 确定性(temp0.9): {outs}")


def check_schema(j):
    ct = j["choices"][0]["message"]["content"]
    try:
        return (json.loads(ct).get("ok") is not None, f"content={ct!r}")
    except Exception:
        return (False, f"非 JSON: {ct!r}")


one("json_schema 一致性",
    {"model": "deepseek-chat", "max_tokens": 40,
     "response_format": {"type": "json_schema", "json_schema": {"name": "ok", "schema": {
         "type": "object", "properties": {"ok": {"type": "boolean"}},
         "required": ["ok"], "additionalProperties": False}}},
     "messages": [{"role": "user", "content": '返回 {"ok": true}'}]},
    check_schema)

tools = [{"type": "function", "function": {
    "name": "get_weather",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]

one("tool_choice=none 行为",
    {"model": "deepseek-chat", "tools": tools, "tool_choice": "none", "max_tokens": 60,
     "messages": [{"role": "user", "content": "上海天气？用工具查"}]},
    lambda j: (not j["choices"][0]["message"].get("tool_calls"),
               f"tool_calls={j['choices'][0]['message'].get('tool_calls')}, content[:30]={(j['choices'][0]['message']['content'] or '')[:30]!r}"))

one("旧版 functions 行为",
    {"model": "deepseek-chat",
     "functions": [{"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
     "function_call": "auto", "max_tokens": 80,
     "messages": [{"role": "user", "content": "上海天气？必须调用函数"}]},
    lambda j: (bool(j["choices"][0]["message"].get("function_call")),
               f"function_call={j['choices'][0]['message'].get('function_call')}"))

for m in ["glm", "glm-5.2"]:
    r = post({"model": m, "max_tokens": 60,
              "messages": [{"role": "user", "content": "你是谁？哪个公司的模型？一句话。"}]})
    if r.status_code == 200:
        j = r.json()
        print(f"  ✅ 隐藏模型 {m}: {(j['choices'][0]['message'].get('content') or '')[:60]!r}")
    else:
        print(f"  ❌ 隐藏模型 {m}: HTTP {r.status_code} {r.text[:100]}")
