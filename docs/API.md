# SJTU LLM Gateway — API 文档

本地 OpenAI 兼容 LLM 转发网关。交大校内 LLM API 经应用内 IKEv2 隧道优先服务；限额、限速或故障时自动切换到可配置的备用供应商（如 DeepSeek 官方）。

- 设计文档：`docs/superpowers/specs/2026-08-31-sjtu-llm-gateway-design.md`
- 实现计划：`docs/superpowers/plans/2026-08-31-sjtu-llm-gateway.md`
- VPN spike 结论：`docs/superpowers/notes/2026-08-31-vpn-spike.md`
- 参数/速率实测脚本：`~/sjtu-probes/probe_*.py`（**本地诊断工具，不入库不入扫描范围**——从环境变量读凭据）

---

## 1. 架构概览

```
本机应用（OpenAI SDK / 任意兼容客户端）
   │  http://127.0.0.1:8000/v1 （Bearer GATEWAY_API_KEY）
   ▼
gateway 容器（FastAPI）── 候选供应商链：sjtu(P1) → deepseek(P2) → …可配置
   │ 共享网络命名空间 (network_mode: service:vpn)
   ▼
vpn 容器（strongSwan IKEv2）
   └─ 仅 202.120.0.0/16 + models.sjtu.edu.cn IP 走隧道（路由表 220 分流），其余直连
```

宿主机只暴露 `127.0.0.1:8000`（Compose 端口发布），容器外不可访问。

## 2. 快速开始

```bash
cp .env.example .env && cp config.example.yaml config.yaml   # 填密钥
docker compose up -d
curl http://127.0.0.1:8000/health                             # 免鉴权健康检查

curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'
```

OpenAI SDK 用法：

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<GATEWAY_API_KEY>")
resp = client.chat.completions.create(model="deepseek-chat",
                                      messages=[{"role": "user", "content": "你好"}])
```

## 3. 认证

| 环境变量 | 行为 |
|---|---|
| `GATEWAY_API_KEY` 为空（默认） | 不鉴权（仅本机可达） |
| `GATEWAY_API_KEY` 非空 | 除 `/health` 外所有端点要求 `Authorization: Bearer <key>`，错误返回 401（OpenAI 错误格式） |

上游各供应商的真实密钥由网关按 `api_key_env` 从环境变量注入，**客户端永远接触不到**。

## 4. 端点参考

### 4.1 `POST /v1/chat/completions`

透传 OpenAI Chat Completions 请求体（仅替换 `model` 为映射后的上游名、重写 `Authorization`）。支持流式与非流式。

**请求**（字段与 OpenAI 一致）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `model` | string，必填 | 客户端可见模型名，见 §6.1；不在任何供应商清单 → 404 `model_not_found` |
| `messages` | array | 角色 system/user/assistant/tool、多轮、图片 `image_url`(base64) 均透传 |
| `stream` | bool | `true` 时 SSE 透传，见下 |
| 其余参数 | — | `temperature`、`tools`、`response_format`、`stop`、`n`、`max_tokens` 等全部透传，上游支持度见 §6.2 |

**非流式响应**：上游原样响应 + 响应头 `X-Gateway-Provider: <实际服务者>`。

**流式响应**：`text/event-stream`，逐块透传，结尾 `data: [DONE]`；`stream_options.include_usage` 可用。响应头同样带 `X-Gateway-Provider`。

**故障切换语义**（对客户端透明）：

| 上游情况 | 网关行为 |
|---|---|
| 429 | 该供应商熔断 60s，立即换下一候选重试 |
| 配额类（402，或任意 4xx 响应体含 quota/insufficient/balance/exhausted/额度/配额） | 熔断 30min，换下一候选 |
| 5xx / 连接失败 / 超时 / SSRF 校验失败 | 熔断 15s，换下一候选 |
| 4xx（请求本身问题） | 原样透传，不切换 |
| 流式已开始（首块已发出）后中断 | 不重试（避免重复输出），透传错误并将该供应商熔断 15s |
| 主动限速排队 > 2s（仅配置了限速的供应商，默认交大 9 次/分） | 软饱和跳过，直接下一候选 |
| 全部候选耗尽 | 本地 502，见错误表 |

熔断到期真半开恢复（冷却到期先只放一个探测请求，应答成功才全量恢复）；交大永远第一优先级（省钱优先）。

### 4.2 `GET /v1/models`

返回候选链中首个可用供应商的实时模型列表（透传上游 `/models`）；全部不可用时回退为配置文件静态清单（`X-Gateway-Provider: config`）。

### 4.3 `GET /health`（免鉴权）

```json
{"status": "ok", "providers": {
  "sjtu":     {"available": true,  "breaker_open": false, "cooldown_remaining": 0.0, "unavailable_reason": null},
  "deepseek": {"available": true,  "breaker_open": false, "cooldown_remaining": 0.0, "unavailable_reason": null}
}}
```

### 4.4 `GET /stats`

每供应商计数器（自进程启动累计）：`requests / success / rate_limited / quota / server_errors / network_errors / switched_away / soft_saturated`。

### 4.5 错误格式（本地生成，OpenAI 风格）

| HTTP | code | 场景 |
|---|---|---|
| 400 | `invalid_request_error` | 请求体缺 `model` 字段 / JSON 解析失败 |
| 401 | `invalid_request_error` | 网关密钥缺失或错误 |
| 404 | `model_not_found` | 模型名不在任何供应商清单 |
| 502 | `all_providers_failed` | 候选链全部不可用（熔断/网络/软饱和） |

所有响应（含错误）均带 `X-Gateway-Provider` 头：`sjtu`/`deepseek`/…/`local`（本地生成）/`config`（静态回退）。

## 5. 配置参考

### 5.1 `config.yaml`（挂载到容器 `/app/config.yaml`，gitignore）

```yaml
listen_host: 0.0.0.0        # 容器内必须 0.0.0.0；宿主侧仅 127.0.0.1 暴露
listen_port: 8000
providers:
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY            # 密钥只从环境变量读，绝不写进配置
    models: [deepseek-chat, deepseek-reasoner, minimax, minimax-m2.7, qwen, qwen3.6-27b, claw]
    priority: 1                           # 越小越优先
    proactive_rate_limit:                 # 可选：主动限速（防打爆上游 10 次/分）
      requests_per_minute: 9              # 必须为正数
      max_wait_seconds: 2                 # 排队超过即漫游备用
      burst: 3
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models: [deepseek-chat, deepseek-reasoner]
    priority: 2
  # 任意 OpenAI 兼容上游按此格式追加；模型名不同时用 model_map 映射：
  # - name: other
  #   base_url: https://example.com/v1
  #   api_key_env: OTHER_API_KEY
  #   model_map: { minimax: "上游实际名", qwen: "上游实际名" }
  #   priority: 3
failover:
  cooldown_429_seconds: 60
  cooldown_quota_seconds: 1800
  cooldown_network_seconds: 15
  connect_timeout_seconds: 10
  read_timeout_seconds: 600               # 长推理（reasoner）需要长超时
```

规则：`base_url` 仅允许 http/https 且不得解析到环回/私有/保留地址（配置加载与每次请求双重校验）；`api_key_env` 指向的环境变量缺失时该供应商标记不可用（启动日志告警），不阻断启动；新增上游**无需改代码**。

### 5.2 `.env`（gitignore，compose 注入）

| 变量 | 用途 |
|---|---|
| `SJTU_API_KEY` | 交大模型服务密钥（申请见交我办） |
| `DEEPSEEK_API_KEY` | 备用 DeepSeek 官方密钥（可选） |
| `GATEWAY_API_KEY` | 网关本地鉴权密钥（空=不鉴权） |
| `VPN_USERNAME` / `VPN_PASSWORD` | jAccount 凭据（vpn 容器建隧道） |
| `VPN_SERVER`（默认 `stu.vpn.sjtu.edu.cn`） | IKEv2 网关 |
| `VPN_ROUTE_SUBNETS`（默认 `202.120.0.0/16`） | 走隧道的网段（逗号分隔） |
| `VPN_EXTRA_HOSTS`（默认 `models.sjtu.edu.cn`） | 额外按域名解析并入隧道的 /32 |

## 6. 上游交大 API 实测参考（2026-08-31 实测）

### 6.1 模型

| 调用名 | 说明 | 实测表现 |
|---|---|---|
| `deepseek-chat` | DeepSeek V4 Flash 非思考 | TTFT ~1.4s，18–31 tok/s |
| `deepseek-reasoner` | DeepSeek V4 Flash 思考（返回 `reasoning_content`） | 直连 ~16 tok/s |
| `minimax` / `minimax-m2.7` | MiniMax-M2.7（回复内嵌 `<think>`，流式含 reasoning token） | 10–21 tok/s |
| `qwen` / `qwen3.6-27b` | Qwen3.6-27B 多模态（base64 图片实测可用） | 15–17 tok/s |
| `claw` | **文档未记载**，DeepSeek 系带思考，vllm tp8 部署 | 可用 |
| `glm` / `glm-5.2` | litellm 配置中存在 | 403（当前团队仅授权 `public-models` 组，需向 hpc@sjtu.edu.cn 申请） |

### 6.2 OpenAI 参数支持度（实测）

- **行为实证支持**：`stop`、`n`、`max_tokens`、`max_completion_tokens`、`logprobs`+`top_logprobs`、`response_format`（json_object 与 json_schema 均严格生效）、`tools`+`tool_choice(auto/none)`、`temperature=0`（确定性）、`system` 角色、图片 `image_url`、`reasoning_content`、`stream_options.include_usage`
- **接受（推断有效/忽略）**：`top_p`、`top_k`、`presence_penalty`、`frequency_penalty`、`user`、`parallel_tool_calls`、`developer` 角色、多轮 `assistant`、`tool` 角色回传、`reasoning_effort`、`service_tier`/`store`/`metadata`（≈忽略）
- **注意**：`seed` 接受但**不保证可复现**（需要确定性请用 `temperature=0`）；旧版 `functions`/`function_call` 接受但**无效**（请用 `tools`）；`logit_bias` **400 拒绝**（上游投机解码不支持）

### 6.3 限速（实测）

| 维度 | 限额 | 实测行为 |
|---|---|---|
| 请求数 | 10 次/分钟 | 滚动 60s 窗口：并发第 9 个请求起 429，429 不占额度，约 30s 自动恢复 |
| token | 300,000/分钟 | 未触达 |
| 周配额 | 1,000,000,000/周 | 未触达；API key 有效期至 2026-09-30 |

网关默认主动限速 9 次/分 + 排队 2s 上限，实测可让上游 429 完全不发生。

## 7. 部署与运维

```bash
docker compose up -d --build     # 全栈启动
docker compose ps                # vpn 应 healthy
docker compose logs -f gateway   # 结构化日志：model=/chain=/provider=/status=/elapsed_ms=/switch_trace=
docker compose logs -f vpn       # 隧道协商/保活/路由
curl -s http://127.0.0.1:8000/health
curl -s -H "Authorization: Bearer $GATEWAY_API_KEY" http://127.0.0.1:8000/stats
cd gateway && uv run pytest      # 85 个测试
```

**排障**：

| 症状 | 处置 |
|---|---|
| 502 `all_providers_failed` | 查 `/stats`：`rate_limited`/`soft_saturated` → 等冷却（默认 60s）；`network_errors` → 查 vpn 日志与隧道 |
| 某模型无备用 → 高峰期 502 | 给备用供应商加 `model_map` 映射该模型 |
| vpn 容器反复重连 | 校外重建镜像需校园网 APT 源（或给 Dockerfile 传 `ARG APT_MIRROR`）；会话锁号最长约 8 分钟，keepalive 会自愈 |
| 隧道在但流量不走隧道 | `docker compose exec vpn ip xfrm policy` 核对选择器与 table 220 路由的 src=VIP |
| 日志排查 | 请求级切换原因在 `switch_trace=`（rate_limited/quota/server/network/ssrf/soft_saturated） |

**安全**：密钥仅经 `.env` 注入（gitignore），日志脱敏（结构化行不含任何密钥/Authorization 值）；上游地址双重 SSRF 校验；VPN 流量选择器只圈交大网段。

## 8. 端到端示例

流式：

```bash
curl -sN http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","stream":true,"messages":[{"role":"user","content":"数到3"}]}'
```

函数调用（实测上游支持）：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"上海天气？"}],
       "tools":[{"type":"function","function":{"name":"get_weather",
       "parameters":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]}}}]}'
```
