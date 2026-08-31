"""交大 LLM 全服务可用性与 token 速率探针（一次性诊断工具）。

凭据只从环境变量读取：SJTU_API_KEY（直连上游）、GATEWAY_API_KEY（走网关）。
用法：set -a; . ./.env; set +a; cd gateway && uv run python ../scripts/probe_sjtu.py [--direct-burst]
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

UPSTREAM = "https://models.sjtu.edu.cn/api/v1"
GATEWAY = "http://127.0.0.1:8000/v1"
MODELS = ["deepseek-chat", "deepseek-reasoner", "minimax", "minimax-m2.7", "qwen", "qwen3.6-27b"]
TIMEOUT = httpx.Timeout(30.0, read=300.0)


def mask(key: str) -> str:
    return f"{key[:3]}…{key[-2:]}" if len(key) > 6 else "(short)"


def auth_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def probe_models_list(base: str, key: str, label: str) -> None:
    print(f"\n== GET {label}/models ==")
    try:
        r = httpx.get(f"{base}/models", headers=auth_headers(key), timeout=30)
        ids = []
        if r.status_code == 200:
            try:
                ids = [m.get("id") for m in r.json().get("data", [])]
            except Exception:
                pass
        print(f"  HTTP {r.status_code} in {r.elapsed.total_seconds()*1000:.0f}ms  ids={ids}")
    except httpx.HTTPError as e:
        print(f"  网络错误: {type(e).__name__}: {e}")


def probe_nonstream(base: str, key: str, model: str, label: str) -> dict:
    print(f"\n== [{label}] {model} 非流式 ==")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "用一句话介绍你自己，然后从1数到5。"}],
        "max_tokens": 200,
    }
    t0 = time.perf_counter()
    try:
        r = httpx.post(f"{base}/chat/completions", headers=auth_headers(key), json=body, timeout=TIMEOUT)
        dt = time.perf_counter() - t0
        if r.status_code != 200:
            print(f"  HTTP {r.status_code} in {dt*1000:.0f}ms  body[:200]={r.text[:200]!r}")
            return {"model": model, "ok": False, "status": r.status_code}
        j = r.json()
        usage = j.get("usage", {})
        content = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
        has_reasoning = bool((j.get("choices") or [{}])[0].get("message", {}).get("reasoning_content"))
        ct, pt = usage.get("completion_tokens"), usage.get("prompt_tokens")
        tps = ct / dt if ct else None
        print(f"  HTTP 200 in {dt*1000:.0f}ms  prompt={pt} completion={ct} tok/s={tps and f'{tps:.1f}'}")
        print(f"  reasoning_content={'有' if has_reasoning else '无'}  reply[:80]={content[:80]!r}")
        return {"model": model, "ok": True, "status": 200, "ms": dt * 1000, "pt": pt, "ct": ct, "tps": tps}
    except httpx.HTTPError as e:
        dt = time.perf_counter() - t0
        print(f"  网络错误 in {dt*1000:.0f}ms: {type(e).__name__}: {e}")
        return {"model": model, "ok": False, "status": type(e).__name__}


def probe_stream(base: str, key: str, model: str, label: str) -> dict:
    print(f"\n== [{label}] {model} 流式 ==")
    body = {
        "model": model,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content": "从1慢慢数到15，每个数字一行。"}],
        "max_tokens": 300,
    }
    t0 = time.perf_counter()
    ttft = None
    chunks = 0
    content_chars = 0
    usage = None
    got_done = False
    try:
        with httpx.stream("POST", f"{base}/chat/completions", headers=auth_headers(key), json=body, timeout=TIMEOUT) as r:
            if r.status_code != 200:
                text = r.read().decode("utf-8", "replace")
                print(f"  HTTP {r.status_code}  body[:200]={text[:200]!r}")
                return {"model": model, "ok": False, "status": r.status_code}
            for line in r.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    got_done = True
                    break
                now = time.perf_counter()
                if ttft is None:
                    ttft = now - t0
                chunks += 1
                try:
                    j = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if j.get("usage"):
                    usage = j["usage"]
                for ch in j.get("choices", []):
                    delta = ch.get("delta", {}) or {}
                    content_chars += len(delta.get("content") or "") + len(delta.get("reasoning_content") or "")
        dt = time.perf_counter() - t0
        ct = (usage or {}).get("completion_tokens")
        gen_time = dt - (ttft or 0)
        tps = ct / gen_time if ct and gen_time > 0 else None
        print(f"  TTFT={(ttft or 0)*1000:.0f}ms 总时长={dt*1000:.0f}ms chunks={chunks} [DONE]={'✓' if got_done else '✗'}")
        print(f"  usage={usage} 生成段 tok/s={tps and f'{tps:.1f}'}（含网络抖动）")
        return {"model": model, "ok": True, "ttft_ms": (ttft or 0) * 1000, "total_ms": dt * 1000,
                "chunks": chunks, "done": got_done, "completion_tokens": ct, "tps": tps}
    except httpx.HTTPError as e:
        print(f"  网络错误: {type(e).__name__}: {e}")
        return {"model": model, "ok": False, "status": type(e).__name__}


def probe_gateway_provider(base: str, key: str, model: str) -> str:
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    r = httpx.post(f"{base}/chat/completions", headers=auth_headers(key), json=body, timeout=TIMEOUT)
    return r.headers.get("x-gateway-provider", "?")


def direct_burst(sjtu_key: str) -> None:
    """直接对上游并发 12 个极小请求，实测限速阈值与恢复节奏。"""
    print("\n== 直连上游限速实测（12 并发小请求，绕过网关） ==")
    body = {"model": "deepseek-chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
    client = httpx.Client(headers=auth_headers(sjtu_key), timeout=60)

    def one(i: int) -> tuple[int, float]:
        t = time.perf_counter()
        r = client.post(f"{UPSTREAM}/chat/completions", json=body)
        return r.status_code, (time.perf_counter() - t) * 1000

    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(one, range(12)))
    counts: dict[int, int] = {}
    for sc, _ in results:
        counts[sc] = counts.get(sc, 0) + 1
    print(f"  状态码分布: {counts}")
    for sc, ms in results:
        print(f"    {sc} in {ms:.0f}ms")

    if any(sc == 429 for sc, _ in results):
        print("  观察限速恢复（每 10s 一个小请求，最多 9 次）…")
        for i in range(9):
            time.sleep(10)
            try:
                r = client.post(f"{UPSTREAM}/chat/completions", json=body)
                print(f"    +{(i+1)*10}s -> HTTP {r.status_code}")
                if r.status_code == 200:
                    break
            except httpx.HTTPError as e:
                print(f"    +{(i+1)*10}s -> {type(e).__name__}")
    client.close()


def main() -> None:
    sjtu_key = os.environ.get("SJTU_API_KEY", "")
    gw_key = os.environ.get("GATEWAY_API_KEY", "")
    if not sjtu_key or not gw_key:
        sys.exit("需要环境变量 SJTU_API_KEY 与 GATEWAY_API_KEY（set -a; . ./.env; set +a）")
    print(f"密钥来源: 环境变量（SJTU={mask(sjtu_key)} GATEWAY={mask(gw_key)}）")

    print("\n######## 1. 模型列表 ########")
    probe_models_list(UPSTREAM, sjtu_key, "上游直连")
    probe_models_list(GATEWAY, gw_key, "网关")

    print("\n######## 2. 逐模型：经网关 非流式 + 流式 ########")
    summary = []
    for m in MODELS:
        ns = probe_nonstream(GATEWAY, gw_key, m, "网关")
        st = probe_stream(GATEWAY, gw_key, m, "网关")
        prov = probe_gateway_provider(GATEWAY, gw_key, m)
        summary.append({"model": m, "provider": prov, "nonstream": ns, "stream": st})

    print("\n######## 汇总表 ########")
    print(f"{'模型':<20}{'实际供应商':<10}{'非流式':<10}{'ms':>7}{'tok/s':>8}  {'流式':<8}{'TTFT':>7}{'总ms':>8}{'tok/s':>8}{'DONE':>5}")
    for s in summary:
        ns, st = s["nonstream"], s["stream"]
        print(f"{s['model']:<20}{s['provider']:<10}"
              f"{'OK' if ns.get('ok') else ns.get('status'):<10}{ns.get('ms', 0):>7.0f}{ns.get('tps') or 0:>8.1f}  "
              f"{'OK' if st.get('ok') else st.get('status'):<8}{st.get('ttft_ms', 0):>7.0f}{st.get('total_ms', 0):>8.0f}"
              f"{st.get('tps') or 0:>8.1f}{'✓' if st.get('done') else '✗':>5}")

    if "--direct-burst" in sys.argv:
        print("\n######## 3. 上游限速阈值 ########")
        direct_burst(sjtu_key)


if __name__ == "__main__":
    main()
