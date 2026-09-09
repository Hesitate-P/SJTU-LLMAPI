# SJTU LLM Gateway — 完整文档

本地 OpenAI 兼容 LLM **聚合网关**：交大校内 LLM API 经应用内 IKEv2 隧道优先服务，按上下文窗口智能路由，限额/限速/故障时自动切换到可配置的备用供应商，并将所有上游归一为**一个完整的 OpenAI 端点**。

> 文档基于 2026-09-09 真实环境全量验证（单元 146 测试 + 20 项真实端到端全通过）。
> 相关文档：[v1 设计](superpowers/specs/2026-08-31-sjtu-llm-gateway-design.md) · [v2 聚合层设计](superpowers/specs/2026-09-06-aggregation-layer-design.md) · [VPN spike 结论](superpowers/notes/2026-08-31-vpn-spike.md)

---

## 目录

1. [架构](#1-架构)
2. [快速开始](#2-快速开始)
3. [端点参考](#3-端点参考)
4. [聚合语义](#4-聚合语义)
5. [故障切换与限速](#5-故障切换与限速)
6. [配置参考](#6-配置参考)
7. [环境变量](#7-环境变量)
8. [上游交大 API 实测参考](#8-上游交大-api-实测参考)
9. [部署与运维](#9-部署与运维)
10. [测试](#10-测试)

## 1. 架构

```
本机应用（OpenAI SDK / 任意兼容客户端 / Claude Code 等）
   │  http://127.0.0.1:8000/v1   Bearer GATEWAY_API_KEY（可关）
   ▼
┌─ gateway 容器（FastAPI，Python 3.13）─────────────────────┐
│ ①鉴权 → ②模型解析(候选链) → ③上下文过滤(窗口/413)          │
│ → ④熔断半开探测 → ⑤主动限速 → ⑥转发(经隧道/直连)           │
│ → ⑦宽切换/熔断 → ⑧协议归一(model回写/think改投) → 客户端   │
│    X-Gateway-Provider / X-Gateway-Context-Limit / /stats  │
└──────────────┬─────────────────────────────────────────────┘
               │ 共享网络命名空间 (network_mode: service:vpn)
┌─ vpn 容器（strongSwan IKEv2）──────────────────────────────┐
│ eap-mschapv2(jAccount) → stu.vpn.sjtu.edu.cn               │
│ 服务端强制 TS 0/0 → table 220 手工分流：仅 202.120.0.0/16   │
│ + models.sjtu.edu.cn IP 走隧道，其余直连；30s 探测自愈+DPD  │
└────────────────────────────────────────────────────────────┘
```

模块划分（`gateway/app/`）：`main`（端点/鉴权/头注入）→ `forward`（转发引擎/宽切换）→ `providers`（候选链）→ `estimate`（token 估算）→ `config`（配置/窗口表/单调校验）→ `normalize`（归一 + 流式状态机）→ `failover`（错误分类/熔断半开）→ `ratelimit`（令牌桶）→ `stats`（计数）→ `security`（SSRF 双重校验）。

宿主机只暴露 `127.0.0.1:8000`（Compose 端口发布），容器外不可访问。

## 2. 快速开始

```bash
cp .env.example .env && cp config.example.yaml config.yaml   # 填密钥
docker compose up -d --build          # 注意 --build：代码更新后必须重建镜像
curl http://127.0.0.1:8000/health     # 免鉴权健康检查（"available":true ×2 即就绪）

# 第一次调用
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'
```

OpenAI SDK：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<GATEWAY_API_KEY>")
r = client.chat.completions.create(model="deepseek-chat",
                                   messages=[{"role": "user", "content": "你好"}])
```

## 3. 端点参考

### 3.1 `POST /v1/chat/completions`

请求体与 OpenAI Chat Completions 一致，网关透传全部参数（`model` 按映射改写后转发）。上游参数支持度见 §8.2。

**响应头**：

| 头 | 含义 |
|---|---|
| `X-Gateway-Provider` | 实际服务者：供应商名 / `local`（本地生成错误）/ `config`（静态目录） |
| `X-Gateway-Context-Limit` | 实际服务者对该模型的上下文窗口（仅成功响应） |

**非流式**：上游响应经归一（见 §4.3）后返回——`model` 为客户端请求名，minimax 的思考在 `reasoning_content`。

**流式**（`stream: true`）：`text/event-stream` 逐事件透传 + 归一，结尾 `data: [DONE]`；`stream_options.include_usage` 可用。流中断时错误透传且该供应商熔断 15s（不会重复输出）。

### 3.2 `GET /v1/models`

**聚合语义**：返回网关配置的客户端可见模型名并集（不透传上游），每项附非标准扩展字段：

```json
{"object": "list", "data": [
  {"id": "deepseek-chat", "object": "model", "owned_by": "gateway", "context_window": 262144},
  {"id": "minimax",       "object": "model", "owned_by": "gateway", "context_window": 196608}
]}
```

`context_window` 取"最优先可服务该模型的供应商"的窗口；`X-Gateway-Provider: config`；不发起任何上游请求。

### 3.3 `GET /health`（免鉴权）

```json
{"status": "ok", "providers": {
  "sjtu":     {"available": true, "breaker_open": false, "cooldown_remaining": 0.0, "unavailable_reason": null},
  "deepseek": {"available": true, "breaker_open": false, "cooldown_remaining": 0.0, "unavailable_reason": null}
}}
```

`available=false` 表示该供应商的密钥环境变量缺失；`breaker_open=true` 表示熔断中（`cooldown_remaining` 为剩余秒数，半开探测中也显示 open）。

### 3.4 `GET /stats`

每供应商累计计数：`requests / success / rate_limited / quota / server_errors / network_errors / switched_away / soft_saturated`。

### 3.5 错误格式（本地生成，OpenAI 风格）

| HTTP | code | 场景 |
|---|---|---|
| 400 | `invalid_request_error` | 缺 `model` 字段 / JSON 解析失败 |
| 401 | `invalid_request_error` | 网关密钥缺失/错误（仅 `/health` 豁免） |
| 404 | `model_not_found` | 模型名不在任何供应商清单 |
| 413 | `context_length_exceeded` | 估算 token 超过**所有**候选窗口（message 含估算值与窗口清单，零上游调用，毫秒级返回） |
| 502 | `all_providers_failed` | 候选全部不可用（熔断/软饱和/网络，含上下文剔除后仅剩者熔断的场景） |

## 4. 聚合语义（v2）

### 4.1 上下文感知路由

1. **窗口声明**：每供应商 `context_window` + 按模型 `model_contexts`；缺省用内置表（交大实测：deepseek/qwen 系 262144、minimax 系 196608；其它供应商**必须显式配置**——裸模型名会被兜成交大值导致误路由）。
2. **请求估算**：`中文段字符×0.6 + ASCII×0.25，×1.1 安全系数，+ 每消息 4，+ max_tokens`。字符启发式，刻意保守（重复字符类输入会高估——宁可错剔不少放）。
3. **链过滤**：估算 > 窗口的供应商**在熔断/限速之前**被剔除（trace 记 `context_too_large`）；全部被剔除 → 本地 413。
4. **兜底单调校验**：同一模型若兜底窗口 < 更优先者，启动 `WARNING`（提示选型，不阻断）。

实测（2026-09-09）：143K 请求 → deepseek 官方(131072)被剔除、由 sjtu(262144) 服务；330K 请求 → 28ms 本地 413。

### 4.2 模型目录聚合

`/v1/models` 即目录（见 §3.2）；新增供应商/模型只改配置不改代码。

### 4.3 协议归一

| 归一项 | 非流式 | 流式 |
|---|---|---|
| `model` 回写客户端请求名 | ✅ | ✅ 逐 SSE 事件 |
| minimax `<think>…</think>` → `reasoning_content` | ✅ 剥离（闭合后 lstrip） | ✅ 两态状态机（闭合标签跨 chunk 缓冲；不 lstrip，不丢字节） |

归一**只应用于成功响应**（4xx 透传保持原貌）；一切归一异常（坏 JSON、未配对代理等）降级为原样字节透出 + warning 日志——绝不因归一丢数据或把 200 变错误。流式 `<think>` 开标签跨事件分裂、非首增量的迟到 `<think>` 不识别（原样透出，已裁决行为）。

## 5. 故障切换与限速

**宽切换矩阵**（对客户端透明，切换只发生在首字节写出之前）：

| 上游情况 | 处置 | 熔断 |
|---|---|---|
| 429 | 换下一候选 | 60s |
| 配额（402 或任意 4xx 响应体含 quota/insufficient/balance/exhausted/额度/配额） | 换下一候选 | 30min |
| 5xx / 连接失败 / 超时 / DNS 故障 / SSRF 校验失败 | 换下一候选 | 15s |
| 其它 4xx（请求问题） | **原样透传，不切换** | 无 |
| 流式首块前失败 | 换下一候选 | 按类 |
| 流式已开始后中断 | 透传错误（不重复输出） | 15s |
| 上下文窗口不足 | 该供应商剔除本请求 | — |
| 全候选耗尽 | 413（全因窗口）或 502 | — |

**熔断恢复（半开）**：冷却到期只放**一个探测请求**（并发者继续走备用），任一响应成功即恢复；探测异常退出有防卡死守卫。

**主动限速**（对配置了 `proactive_rate_limit` 的供应商，默认交大）：令牌桶 9 次/分（burst 3），排队上限 2s——超时即软饱和漫游备用，实测可让上游 429 完全不发生。

**优先级**：交大永远第一（免费优先）；备用仅在其暂不可用/吃不下时服务。

## 6. 配置参考

### 6.1 `config.yaml`（完整字段）

```yaml
listen_host: 0.0.0.0        # 容器内必须 0.0.0.0；仅 python -m app.main 本地直跑时生效
listen_port: 8000

providers:                  # 按此格式可加任意 OpenAI 兼容上游
  - name: sjtu              # 供应商名（X-Gateway-Provider 与日志用）
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY       # 密钥环境变量名（绝不写值）
    models: [deepseek-chat, deepseek-reasoner, minimax, minimax-m2.7, qwen, qwen3.6-27b, claw]
    priority: 1                    # 越小越优先；同模型跨供应商按此排序
    context_window: 262144         # 可选：供应商级窗口；缺省用内置表
    model_contexts: { minimax: 196608 }   # 可选：按模型覆盖
    proactive_rate_limit:          # 可选：主动限速
      requests_per_minute: 9       # 必须为正
      max_wait_seconds: 2
      burst: 3
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models: [deepseek-chat, deepseek-reasoner]
    priority: 2
    context_window: 131072  # 非 sjtu 供应商务必显式声明！
    # model_map: { minimax: "上游实际模型名" }   # 模型名不同时的映射（同时注册该模型到目录）

failover:
  cooldown_429_seconds: 60
  cooldown_quota_seconds: 1800
  cooldown_network_seconds: 15
  connect_timeout_seconds: 10
  read_timeout_seconds: 600       # reasoner 长推理需要
```

**加载校验**：`base_url` 仅 http/https 且不得解析到环回/私有/保留地址（双重：加载时 + 每请求时）；路径只允许工作目录/`/app`/临时目录；`api_key_env` 缺失 → 供应商标记不可用（启动 WARNING）不阻断；`requests_per_minute/burst` 必须为正；窗口兜底单调性 WARNING。

### 6.2 上下文窗口解析顺序

`model_contexts[model]` → `context_window` → 内置表（交大实测值）→ 8192。

## 7. 环境变量

| 变量 | 用途 | 默认 |
|---|---|---|
| `SJTU_API_KEY` | 交大密钥（必需） | — |
| `DEEPSEEK_API_KEY` | 备用 DeepSeek 官方 | 未设则该供应商不可用 |
| `GATEWAY_API_KEY` | 网关鉴权；**空=不鉴权**（仅本机可达） | 空 |
| `VPN_USERNAME` / `VPN_PASSWORD` | jAccount（vpn 容器） | 必填 |
| `VPN_SERVER` | IKEv2 网关 | `stu.vpn.sjtu.edu.cn` |
| `VPN_ROUTE_SUBNETS` | 走隧道的网段 | `202.120.0.0/16` |
| `VPN_EXTRA_HOSTS` | 额外解析并入隧道的域名 | `models.sjtu.edu.cn` |

## 8. 上游交大 API 实测参考

### 8.1 模型（2026-09 实测）

| 调用名 | 说明 | 实测 |
|---|---|---|
| `deepseek-chat` | DeepSeek V4 Flash | TTFT ~1.4s，18–31 tok/s，256K |
| `deepseek-reasoner` | 思考模式（`reasoning_content`） | ~16 tok/s，256K |
| `minimax` / `minimax-m2.7` | MiniMax-M2.7（原生思考） | 10–21 tok/s，192K |
| `qwen` / `qwen3.6-27b` | Qwen 多模态（base64 图片✅） | 15–17 tok/s，256K |
| `claw` | **未记载**，DeepSeek 系思考，vllm tp8 | 可用 |
| `glm` / `glm-5.2` | litellm 配置存在 | 403（需申请授权） |

### 8.2 OpenAI 参数支持度（实测）

- **行为实证**：`stop` `n` `max_tokens` `max_completion_tokens` `logprobs(+top)` `response_format`(json_object/json_schema) `tools`+`tool_choice`(auto/none) `temperature=0`(确定性) `system` `image_url` `stream_options.include_usage`
- **接受（未证/忽略）**：`top_p` `top_k` `presence/frequency_penalty` `user` `parallel_tool_calls` `developer` 角色 多轮 `assistant` `tool` 回传 `reasoning_effort`；`service_tier/store/metadata`≈忽略
- **注意**：`seed` 不保证可复现（用 temperature=0）；旧版 `functions` 无效（用 `tools`）；`logit_bias` **400**（上游投机解码）

### 8.3 限速（实测）

| 维度 | 值 | 实测行为 |
|---|---|---|
| 请求 | 10 次/分 | 滚动 60s 窗口：并发第 9 个起 429，约 30s 恢复，429 不占额度 |
| token | 300K/分 | 未触达 |
| 周 | 1B/周 | 未触达；key 有效期至 **2026-09-30**（注意续期） |

网关主动限速 9 次/分 + 2s 排队上限，实测上游 429 零发生。

## 9. 部署与运维

```bash
docker compose up -d --build     # ⚠️ 代码更新后必须 --build（up -d 不会重建）
docker compose ps                # vpn 应 healthy
docker compose logs -f gateway   # 结构化：model=/chain=/provider=/status=/elapsed_ms=/switch_trace=
docker compose logs -f vpn       # 隧道协商/保活/路由（TS、VIP、table 220）
cd gateway && uv run pytest      # 146 个单元/集成测试
```

**排障**：

| 症状 | 处置 |
|---|---|
| 改了代码行为没变 | 镜像没重建——`docker compose up -d --build` |
| 502 `all_providers_failed` | `/stats` 看 `rate_limited/soft_saturated`（等 60s 冷却）或 `network_errors`（查 vpn 日志） |
| 413 `context_length_exceeded` | 请求确实超窗：换模型/裁剪，或评估给该模型配更大窗口的兜底 |
| 某模型高峰 502 | 无兜底供应商服务该模型——给备用加 `model_map` |
| vpn 反复重连 | 会话锁号最长 ~8 分钟自愈；校外重建镜像需校园网 APT 源（`ARG APT_MIRROR` 可覆盖） |
| 疑似流量未走隧道 | `docker compose exec vpn ip xfrm policy`（选择器）/ `ip route show table 220`（src=VIP） |
| 容器起不来 exec 报 no such file | Windows 检出 CRLF——`.gitattributes` 已强制 LF，重新检出：`git rm --cached -r . && git reset --hard` |

**安全**：密钥仅 `.env`（gitignored）；日志脱敏（结构化行不含密钥/Authorization）；上游双重 SSRF 校验；配置路径白名单；VPN 仅交大网段走隧道；CA 唯一信任锚（ISRG Root X1）。

## 10. 测试

- **单元/集成**：`cd gateway && uv run pytest`（146：估算器/窗口/过滤/413/归一/状态机/熔断半开/流式生命周期/SSRF/鉴权/日志脱敏等；1 条已知第三方弃用 warning）。
- **真实端到端**：`~/sjtu-probes/live_v2_test.py`（本地工具不入库）——20 项断言覆盖 models 聚合、上下文路由（143K/330K）、归一（非流式+流式）、响应头；另有 `probe_sjtu.py`（全模型可用性/速率）、`probe_openai_params.py`（参数支持度）：
  ```bash
  set -a; . ./.env; set +a; cd gateway && uv run python ~/sjtu-probes/live_v2_test.py
  ```
- **安全扫描**：mimosa 深度扫描 0 findings（seal `sha256:a8b79ddb…`，2026-09-09）。
