# v2 聚合层实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 网关升级为轻量聚合层：上下文感知路由（窗口声明/链过滤/413）+ 响应协议归一（model 回写、`<think>`→`reasoning_content` 含流式状态机）+ models 聚合并集。

**Architecture:** 在现有 v1 模块上增量扩展：新增 `app/estimate.py`（token 估算）与 `app/normalize.py`（归一 + 流式状态机）；`config.py` 增窗口声明与单调校验；`forward.py` 在候选链后按窗口过滤并在返回前归一；`main.py` 注入 `X-Gateway-Context-Limit` 并聚合 models。故障切换/熔断/限速/鉴权语义不变。

**Tech Stack:** 同 v1（Python 3.13 + FastAPI + httpx + pytest/pytest-asyncio）。

**Spec:** `docs/superpowers/specs/2026-09-06-aggregation-layer-design.md`

## Global Constraints

- 现有 98 测试零破坏（新增测试另计）。
- 归一异常一律降级原样透出，绝不丢数据；降级记 warning 日志。
- 密钥只从环境变量读取（mimosa 约束）；上游请求前校验 host 拒绝环回/私有/保留地址（既有 SSRF 双重校验不回退）。
- 流式归一在首块提交语义之后进行，不得改变故障切换边界。
- 估算公式固定：中文(码点>0x2E80)×0.6 + ASCII×0.25，求和×1.1，+ max_tokens（`max_tokens` 或 `max_completion_tokens`，有则取其值），+ 每消息 4。
- 每任务：TDD（先红后绿）→ conventional commit；测试命令 `cd gateway && uv run pytest`。

---

### Task 1: token 估算器（app/estimate.py）

**Files:**
- Create: `gateway/app/estimate.py`
- Test: `gateway/tests/test_estimate.py`

**Interfaces:**
- Produces: `estimate_request_tokens(body: dict) -> int`（FR11 公式）。`content` 兼容 str 与 OpenAI parts 数组（取 `text` 段字符）；`max_tokens`/`max_completion_tokens` 缺省 0。

- [ ] **Step 1: 失败测试**

```python
from app.estimate import estimate_request_tokens


def msg(content):
    return {"messages": [{"role": "user", "content": content}]}


def test_pure_ascii():
    body = msg("a" * 100)
    # (100*0.25)*1.1 + 4 = 27.5 + 4 → 31.5 → int
    assert estimate_request_tokens(body) == int(100 * 0.25 * 1.1) + 4


def test_pure_chinese():
    body = msg("中" * 100)   # 码点 > 0x2E80
    assert estimate_request_tokens(body) == int(100 * 0.6 * 1.1) + 4


def test_mixed_and_parts():
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "你好"}, {"type": "text", "text": "world"},
        {"type": "image_url", "image_url": {"url": "data:..."}}]}]}
    zh, en = 2 * 0.6, 5 * 0.25
    assert estimate_request_tokens(body) == int((zh + en) * 1.1) + 4


def test_max_tokens_counted():
    body = {**msg("hi"), "max_tokens": 500}
    assert estimate_request_tokens(body) == int(2 * 0.25 * 1.1) + 4 + 500


def test_max_completion_tokens_equivalent():
    body = {**msg("hi"), "max_completion_tokens": 500}
    assert estimate_request_tokens(body) == int(2 * 0.25 * 1.1) + 4 + 500


def test_empty_messages():
    assert estimate_request_tokens({"messages": []}) == 0
```

- [ ] **Step 2: 跑测确认失败**（ModuleNotFoundError）
- [ ] **Step 3: 实现**

```python
"""请求 token 估算（FR11）：字符启发式，够做路由决策即可。"""
from __future__ import annotations

_PER_MESSAGE = 4
_ZH_FACTOR, _ASCII_FACTOR, _SAFETY = 0.6, 0.25, 1.1


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def estimate_request_tokens(body: dict) -> int:
    base = 0.0
    for m in body.get("messages") or []:
        text = _text_of(m.get("content") if isinstance(m, dict) else None)
        zh = sum(1 for ch in text if ord(ch) > 0x2E80)
        base += zh * _ZH_FACTOR + (len(text) - zh) * _ASCII_FACTOR
    max_out = body.get("max_tokens") or body.get("max_completion_tokens") or 0
    return int(base * _SAFETY + _PER_MESSAGE * len(body.get("messages") or []) + max_out)
```

- [ ] **Step 4: 跑测绿 + 全量回归 → 提交 `feat: 请求 token 估算器`**

---

### Task 2: 窗口声明与单调校验（config 扩展）

**Files:**
- Modify: `gateway/app/config.py`
- Test: `gateway/tests/test_config.py`（追加）

**Interfaces:**
- Produces:
  - `ProviderConfig` 新字段 `context_window: int | None = None`、`model_contexts: dict[str, int]`（默认空）
  - 模块级 `DEFAULT_CONTEXT_WINDOWS: dict[str, int]`（键为 "sjtu:deepseek-chat" 形式的 `provider:model` 与裸 model 兜底 8192）：交大 deepseek-chat/deepseek-reasoner/qwen/qwen3.6-27b=262144、minimax/minimax-m2.7=196608（对 sjtu 与任意供应商的同名模型同样适用）、DeepSeek 官方渠道按其模型名 131072 由显式配置声明（不内置按供应商名的假设，避免误配）
  - `context_for(provider: ProviderConfig, model: str) -> int`：model_contexts[model] → provider.context_window → DEFAULT_CONTEXT_WINDOWS[f"{provider.name}:{model}"] → DEFAULT_CONTEXT_WINDOWS[model] → 8192
  - load_config 末尾 FR10 单调校验：按"客户端可见模型"分组（遍历 providers 的 models+model_map 键），组内按 priority 升序，窗口严格递减处 `logger.warning("模型 %s 的兜底 %s 窗口 %d 小于 %s 的 %d", ...)`

- [ ] **Step 1: 失败测试**（追加）：
```python
def test_context_resolution_priority(tmp_path):
    # model_contexts > context_window > 内置表 > 8192
    ...构造 yaml：provider A 有 context_window: 100000 与 model_contexts: {m1: 50000}
    断言 context_for(A, "m1") == 50000、context_for(A, "m2") == 100000
    provider B（sjtu，未配置）context_for(B, "deepseek-chat") == 262144、context_for(B, "minimax") == 196608
    provider C context_for(C, "unknown-model") == 8192


def test_monotonic_warning(tmp_path, caplog):
    # sjtu(256K 内置) → small(配置 65536)，同服务 deepseek-chat
    ...load_config 后 caplog 有 "窗口" WARNING 且含两家名字
    # 窗口单调时（兜底更大）无该 WARNING
```
- [ ] **Step 2: 红 → 实现（字段+表+context_for+校验）→ 绿**
- [ ] **Step 3: 全量回归 → 提交 `feat: 供应商上下文窗口声明与单调校验`**

---

### Task 3: 候选链上下文过滤与 413（forward）

**Files:**
- Modify: `gateway/app/forward.py`
- Test: `gateway/tests/test_forward.py`（追加）

**Interfaces:**
- Consumes: `estimate_request_tokens`、`context_for`
- Produces:
  - `GatewayResponse` 新字段 `context_limit: int | None = None`（成功路径设为实际服务者窗口）
  - chat() 在 build_chain 之后：`need = estimate_request_tokens(request_body)`；对每个 `(provider, upstream_model)` 检查 `context_for(provider, request_body["model"])`（注意用**客户端模型名**查窗口，各家对同一逻辑模型窗口一致），`need > window` 则跳过并 `trace.append((provider.name, "context_too_large"))` + `logger.info`；链耗尽且曾因窗口剔除 → 返回本地 413：
```python
GatewayResponse(413, "application/json", "local",
    body=_openai_error(f"请求估算 {need} tokens 超过所有候选供应商的上下文窗口：{窗清单}",
                       "invalid_request_error", "context_length_exceeded"), context_limit=None)
```
  - 窗口剔除发生在熔断/限速检查**之前**（物理不可能的供应商不占探测与令牌）

- [ ] **Step 1: 失败测试**：
  - `test_context_filtered_to_capable_provider`：sjtu 窗 262144（内置）、deepseek 显式 131072；请求体含长文本（约 130K token：`"a"*520000` → 520000*0.25*1.1≈143000）且 breaker 把 sjtu 打开时……注意语义：130K 时 deepseek 被剔除、sjtu 保留——正常由 sjtu 服务（X 头后验）；再构造 sjtu 熔断场景断言不会打到 deepseek（calls 里无 deepseek.test）且返回 502（链只剩 sjtu 且熔断）而非上游 400。
  - `test_all_too_large_returns_413`：`"a"*1200000`（≈330K）→ 413、body 含 `context_length_exceeded`、不打任何上游。
  - 既有小请求用例零改动仍绿。
- [ ] **Step 2: 红 → 实现 → 绿 → 全量回归 → 提交 `feat: 候选链上下文过滤与本地 413`**

---

### Task 4: 非流式响应归一（app/normalize.py）

**Files:**
- Create: `gateway/app/normalize.py`
- Modify: `gateway/app/forward.py`（非流式 OK 返回前应用）
- Test: `gateway/tests/test_normalize.py`

**Interfaces:**
- Produces: `normalize_response(j: dict, client_model: str) -> dict`——
  1. `j["model"] = client_model`；
  2. choices[*].message.content 若为 str 且以 `<think>` 开头且含 `</think>`：闭合前的内容（去标签）追加/新建 `reasoning_content`（已有则前缀拼接），content 为闭合后剩余（strip 前导空白）；未闭合 → 原样不动。
  3. 任何异常 → 返回原 j（降级）。
- forward `_attempt` 非流式分支：`OK` 分类后、构造 GatewayResponse 前应用（CLIENT 透传不归一——保持错误原貌）。

- [ ] **Step 1: 失败测试**：model 回写；完整 think 剥离（content=后文、reasoning=思考）；未闭合保留；已有 reasoning_content 前拼；异常输入（choices 缺失 dict）原样返回。
- [ ] **Step 2: 红 → 实现 → 绿 → 提交 `feat: 非流式响应归一（model 回写与 think 剥离）`**

---

### Task 5: 流式归一状态机（StreamNormalizer）

**Files:**
- Modify: `gateway/app/normalize.py`
- Modify: `gateway/app/forward.py`（流 generator 逐 data: 行过 normalizer）
- Test: `gateway/tests/test_normalize.py`（追加）

**Interfaces:**
- Produces:
```python
class StreamNormalizer:
    """SSE data: 行级归一：model 回写 + <think>→reasoning_content 增量改投。

    THINK/NORMAL 两态 + 半标签缓冲（</think> 可能跨 chunk 分裂）。
    解析失败一律原样返回该行并记 warning（降级，绝不丢数据）。
    """
    def __init__(self, client_model: str): ...
    def feed(self, line: str) -> str: ...   # 输入含 "data: " 前缀的完整 SSE 行，输出同格式
    def finish(self) -> str | None: ...     # 流结束时冲刷半标签缓冲（残余按原样补发）
```
  - 行为：非 `data:` 前缀行直通；`data: [DONE]` 直通；JSON 解析失败 → 原样 + warning；THINK 态下 delta.content 增量改投 delta.reasoning_content（content 置 ""）；缓冲逻辑：THINK 态下若缓冲+增量中仍未出现 `</think>`，输出安全前缀、滞留可能构成半标签的尾部（≤7 字符，即 `</think>` 长度-1）；出现闭合则切换 NORMAL 并把其后内容作为 content 输出。
  - 集成：forward 流 generator 中，首块**提交前**的首块也须过 normalizer（首块属 THINK 态时整块改投），其后每 chunk 逐行 feed（按 `\n` splitlines 保留行结构重组）。

- [ ] **Step 1: 失败测试**（纯状态机，不经网络）：
  - 完整 think 单事件：content 增量 → reasoning 增量；
  - `</th` + `ink>` 跨两事件分裂：缓冲正确、无内容丢失或重复；
  - 无 think 流直通（仅 model 回写）；
  - think 后正常 delta 走 content；
  - 坏 JSON 行原样返回；
  - finish() 冲刷残余缓冲；
  - model 回写对每个事件生效。
- [ ] **Step 2: 红 → 实现 → 绿 → 提交 `feat: 流式归一状态机（model 回写与 think 改投）`**

---

### Task 6: 头注入与 models 聚合（main）

**Files:**
- Modify: `gateway/app/main.py`
- Test: `gateway/tests/test_main.py`（追加）

**Interfaces:**
- Consumes: `GatewayResponse.context_limit`
- Produces: `_to_response` 在 context_limit 非 None 时加 `X-Gateway-Context-Limit` 头；`GET /v1/models` 改聚合语义：遍历 `cfg.providers`（available 与否都算目录）取 `models + model_map` 键并集，每项 `{"id": m, "object": "model", "owned_by": "gateway", "context_window": context_for(最优先可服务该模型的 provider, m)}`，provider 标 `"config"`；删除 forward.list_models 的上游拉取路径或仅保留为内部未用（**删除**，简化）。

- [ ] **Step 1: 失败测试**：models 返回并集含 model_map 键与 context_window 字段；chat 成功响应带 X-Gateway-Context-Limit。
- [ ] **Step 2: 红 → 实现 → 绿 → 全量回归（test_main/test_integration 中引用 list_models 的用例同步更新）→ 提交 `feat: models 聚合与上下文窗口头`**

---

### Task 7: 集成测试与文档

**Files:**
- Test: `gateway/tests/test_integration_v2.py`（新）
- Modify: `docs/API.md`

**Interfaces:**
- Consumes: 全部前序。

- [ ] **Step 1: 集成测试**（MockTransport，resolver 注入公网 IP）：
  - 超窗路由：`"a"*520000`（≈143K）请求 deepseek-chat（sjtu 内置 256K + deepseek 配 131072）→ 200 且 `X-Gateway-Provider: sjtu`、`X-Gateway-Context-Limit: 262144`；handler 断言 deepseek.test 未被调用。
  - 全超窗：`"a"*1200000` → 413，上游零调用。
  - 流式归一 e2e：minimax 上游 mock 吐 `<think>` 前缀分块流 → 断言客户端收到的 SSE 每个 data 行 model==客户端名、think 段在 reasoning 增量、content 干净、[DONE] 在尾。
  - models 聚合：/v1/models 返回配置并集+context_window。
- [ ] **Step 2: docs/API.md**：§4.1 增上下文过滤与 413 行、`X-Gateway-Context-Limit` 头说明；§4.2 改聚合语义；§5.1 配置示例补 context_window/model_contexts；§6 增归一行为说明（minimax 思考字段、model 回写）。
- [ ] **Step 3: 全量回归 → 提交 `test: v2 聚合层集成测试与文档`**

---

## Self-Review

- 规格覆盖：FR9(T2) FR10(T2) FR11(T1/T3) FR12(T4) FR13(T5) FR14(T6)；验收 1-5 对应各任务测试与 T7。
- 类型一致：`estimate_request_tokens(body)->int`、`context_for(provider, model)->int`、`GatewayResponse.context_limit`、`StreamNormalizer.feed/finish` 全文一致。
- 无占位符：Task 2/3 的测试描述给出构造要点与断言语义（完整测试代码由实现者按要点先写后跑红）。
