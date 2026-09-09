# sjtu-llm-gateway v2 聚合层设计文档

- 日期：2026-09-06
- 状态：已获用户批准（含流式 `<think>` 状态机全量方案）
- 前置：v1 设计 `2026-08-31-sjtu-llm-gateway-design.md`（本文增量扩展，编号续接 FR9+）

## 1. 背景

v1 网关是"透明路由 + 故障切换"：上下文长度无感知（超窗请求打给上游吃裸 400 且不切换）、协议差异透传（minimax 的 `<think>` 混在 content、`model` 回显上游真名）、`/v1/models` 透传单家上游列表。v2 将其升级为**轻量聚合层**：对齐各供应商能力，聚合为一个完整 OpenAI 端点。参考 new-api 的聚合语义，但不做计费/UI/多用户（单用户本机，YAGNI；将来可在前置架 new-api）。

## 2. 需求

- **FR9 上下文窗口声明**：每供应商可配 `context_window`（整数 token）与 `model_contexts`（按模型覆盖）；缺省用内置表：交大 deepseek-chat/deepseek-reasoner/qwen/qwen3.6-27b=262144、minimax/minimax-m2.7=196608、DeepSeek 官方 deepseek-chat/deepseek-reasoner=131072。
- **FR10 兜底窗口单调校验**：对同一客户端可见模型，候选链中优先级靠后（兜底）的窗口小于靠前者 → 启动 `WARNING` 日志（指出模型、两家窗口值）；不拒绝启动。
- **FR11 请求时上下文过滤**：估算请求 token（中文段字符×0.6 + ASCII 段×0.25，求和 ×1.1，+ max_tokens（有则）+ 每消息固定 4）；候选链剔除 `估算 > 窗口` 的供应商（trace 记 `context_too_large`）；全剔除 → 本地 **413**（OpenAI 错误格式，`code="context_length_exceeded"`，message 含估算值与各候选窗口）；成功响应带 `X-Gateway-Context-Limit: <实际服务者窗口>`。
- **FR12 非流式响应归一**：`model` 字段回写为客户端请求的模型名；`<think>…</think>` 前缀剥离进 `reasoning_content` 字段（仅当 content 以 `<think>` 开头且含闭合标签；未闭合或缺失则原样保留 content）。
- **FR13 流式响应归一**：逐 SSE 事件（`data:` 行）解析 JSON 后回写 `model`；`<think>` 状态机：进入流后处于 THINK 态，`</think>` 之前的增量 content 改投 `reasoning_content` 增量（content 置空串），遇闭合标签切 NORMAL 态；标签跨 chunk 分裂由缓冲半标签处理。**归一异常一律降级**：该事件原样透出，绝不丢数据；降级计入 warning 日志。
- **FR14 `/v1/models` 聚合**：返回网关配置的客户端可见模型名并集（不再透传上游实时列表），每项附非标准扩展字段 `context_window`（取候选链中最优先可服务者的窗口）。

## 3. 非目标

计费/配额/多用户/管理 UI/渠道多密钥池化；不改变 v1 故障切换、熔断、限速、鉴权语义；不做 tiktoken 精确分词。

## 4. 组件设计

### 4.1 `app/estimate.py`（新）
`estimate_request_tokens(body: dict) -> int`——按 FR11 公式；`content` 为字符串或 parts 数组（取 text 段字符）都处理；中文判定按码点 > 0x2E80 粗分。

### 4.2 `app/config.py`（扩展）
ProviderConfig 增 `context_window: int | None`、`model_contexts: dict[str, int]`；加载时应用内置缺省表；load_config 末尾执行 FR10 单调校验（logger.warning）。

### 4.3 供应商窗口查询
`provider context_for(provider, model) -> int`（model_contexts 优先，次 context_window，再内置表，最后 8192 兜底）。

### 4.4 `app/forward.py`（扩展）
chat() 在候选链构造后：计算估算值 → 过滤链（剔除记 trace）→ 空链且原链非空 → 413 GatewayResponse（provider="local"）；正常路径响应头由 main 注入 `X-Gateway-Context-Limit`（GatewayResponse 增加 `context_limit: int | None` 字段）。

### 4.5 `app/normalize.py`（新）
- `normalize_response(j: dict, client_model: str) -> dict`：FR12。
- `StreamNormalizer`：有状态对象。`feed(line: str) -> str`：输入一个 SSE data 行，输出改写后的 data 行；THINK/NORMAL/缓冲逻辑；解析失败 → 原样返回该行并记 warning。`finish()` 清缓冲。
- 集成：forward 非流式路径在返回前归一；流式 generator 的每个 `data:` 行先过 normalizer 再 yield（**在首块提交语义之后**，不影响故障切换边界）。

### 4.6 `app/main.py`
`/v1/models` 改为聚合语义（遍历配置 providers 的 models+model_map 键并集 + context_window）；`_to_response` 注入 `X-Gateway-Context-Limit`。

## 5. 错误矩阵新增

| 情况 | 行为 |
|---|---|
| 估算 token > 全部候选窗口 | 本地 413 `context_length_exceeded`（含估算值与窗口清单） |
| 归一解析失败（单事件） | 原样透出 + warning 日志 |

## 6. 测试策略

单元：估算器（纯中文/纯 ASCII/混合/图片 parts/max_tokens 有无）；窗口查询优先级；单调校验告警；413 路径；非流式归一（model 回写、think 剥离、未闭合保留）；流式状态机（标签完整/跨 2 chunk 分裂/无 think 流/think 后正常 delta/坏 JSON 降级）；models 聚合并集。集成：超窗请求被路由到吃得下的兜底（断言 X-Gateway-Provider 与 413 分支）、流式归一端到端（X-Gateway-Context-Limit、model 回写、reasoning 聚合）。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| 流式逐事件解析的性能开销 | 仅 data: 行解析；非 data 行直通；微基准在集成测试覆盖 |
| SSE 假设（单行 JSON、data: 前缀）与上游不符 | 降级路径保证原样透出；实测两家均符合 |
| think 状态机 bug 丢内容 | 异常与未闭合统一降级原样；专项跨 chunk 测试 |
| 估算过粗导致误剔除 | ×1.1 安全系数 + 413 信息透明 + 窗口可配置调大 |

## 8. 验收标准

1. 现有 98 测试零破坏 + 新增全绿。
2. 构造 130K token 请求：deepseek-chat 候选链（sjtu 256K → deepseek 128K）自动剔除 deepseek，由 sjtu 服务；构造 300K 请求 → 本地 413，不打上游。
3. minimax 非流式/流式响应的 content 无 `<think>`，思考在 `reasoning_content`；`model` 字段回显客户端请求名（流式逐 chunk）。
4. `/v1/models` 返回配置并集 + context_window 字段。
5. 兜底窗口小于交大时启动日志有 WARNING。
