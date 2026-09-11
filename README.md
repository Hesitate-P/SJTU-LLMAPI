# SJTU LLM Gateway

本地 **OpenAI 兼容 LLM 聚合网关**：把上海交通大学 LLM 模型服务（[models.sjtu.edu.cn](https://models.sjtu.edu.cn)）优先接入，经应用内 strongSwan IKEv2 隧道解决校园网限制；按上下文窗口智能路由（超窗请求毫秒级 413 保护）；限速 / 配额 / 故障时自动切换到可配置的备用供应商；所有上游响应归一为一个标准 OpenAI 端点。

客户端只需把 OpenAI SDK 的 `base_url` 指向 `http://127.0.0.1:8000/v1`，即可用 `deepseek-chat`、`minimax`、`qwen` 等模型名无感调用——交大免费额度永远优先，出问题自动落到备用。

> 📖 完整文档（端点参考 / 聚合语义 / 故障切换 / 配置全字段 / 运维排障 / 测试）：[docs/API.md](docs/API.md)

## 特性

- **校园网自动穿透**：独立 vpn 容器用 jAccount 建立 IKEv2 隧道，仅交大网段（`202.120.0.0/16` + API 域名）走隧道，其余流量直连；30s 探测自愈 + DPD 保活
- **上下文窗口感知路由**：请求按字符启发式估算 token，超出某供应商窗口就在候选链中剔除它；全部超窗 → 本地 413（零上游调用）
- **自动故障切换**：429 → 冷却 60s；配额耗尽 → 30min；5xx/网络错误 → 15s。熔断半开探测恢复，切换对客户端完全透明（仅发生在首字节写出前）
- **主动限速**：对交大 10 次/分的限制做令牌桶整形（9 次/分 + 2s 排队上限），实测让上游 429 零发生
- **协议归一**：`model` 字段回写客户端请求名；minimax 的 `<think>…</think>` 剥离为 `reasoning_content`（流式用两态状态机，标签跨 chunk 不丢字节）
- **聚合模型目录**：`GET /v1/models` 返回所有供应商模型名并集，附 `context_window` 扩展字段
- **纵深安全**：密钥只存 `.env`（gitignored）；网关仅监听宿主机 `127.0.0.1:8000`；上游 SSRF 双重校验；VPN CA 唯一信任锚（ISRG Root X1）；日志脱敏

## 架构

```
本机应用（OpenAI SDK / Claude Code 等）
   │  http://127.0.0.1:8000/v1   Bearer GATEWAY_API_KEY（可关）
   ▼
┌─ gateway 容器（FastAPI，Python 3.13）──────────────────────┐
│ 鉴权 → 模型解析(候选链) → 上下文过滤 → 熔断/半开探测       │
│ → 主动限速 → 转发 → 宽切换 → 协议归一 → 客户端             │
└──────────────┬─────────────────────────────────────────────┘
               │ 共享网络命名空间 (network_mode: service:vpn)
┌─ vpn 容器（strongSwan IKEv2）──────────────────────────────┐
│ jAccount (eap-mschapv2) → stu.vpn.sjtu.edu.cn              │
│ table 220 手工分流：仅交大网段走隧道，其余直连              │
└────────────────────────────────────────────────────────────┘
```

## 前置条件

| 条件 | 说明 |
|---|---|
| **jAccount 账号** | 交大统一身份认证，供 vpn 容器建立隧道（用户名 + 密码） |
| **交大 API 密钥** | 向学校申请：[官方文档](https://claw.sjtu.edu.cn/guide/sjtu-api/) · 咨询 hpc@sjtu.edu.cn（网络信息中心）。注意密钥有有效期，到期需续期 |
| **Docker + Compose v2** | 宿主机需 Linux 内核提供 `/dev/net/tun`（WSL2 实测可用；compose 文件已声明 `NET_ADMIN`） |
| 备用供应商密钥（可选） | 如 DeepSeek 官方 `DEEPSEEK_API_KEY`；不配则该供应商标记不可用，交大仍可独立服务 |
| uv（可选） | 仅本地跑测试需要 |

交大 API 限速 10 次/分、300K token/分、1B token/周——网关已内置主动整形，无需自己处理 429。

## 快速部署

```bash
# 1. 克隆
git clone https://github.com/Hesitate-P/SJTU-LLMAPI.git
cd SJTU-LLMAPI

# 2. 创建配置（两文件均已 gitignore，不会入库）
cp .env.example .env
cp config.example.yaml config.yaml

# 3. 编辑 .env，填入密钥
#    SJTU_API_KEY    交大 API 密钥（必需）
#    VPN_USERNAME    jAccount 用户名（必需）
#    VPN_PASSWORD    jAccount 密码（必需）
#    DEEPSEEK_API_KEY  备用 DeepSeek 官方密钥（可选）
#    GATEWAY_API_KEY   网关鉴权密钥；留空 = 不鉴权（端口只绑 127.0.0.1）
vi .env

# 4. 构建 & 启动（⚠️ 改代码后必须带 --build，up -d 不会重建镜像）
docker compose up -d --build

# 5. 等待就绪：vpn 健康检查有 45s start_period，首次约 1 分钟
docker compose ps                      # vpn 应显示 healthy
curl http://127.0.0.1:8000/health      # 免鉴权；两供应商务必 "available": true
```

`/health` 返回示例（`available: false` 表示该供应商密钥环境变量缺失）：

```json
{"status": "ok", "providers": {
  "sjtu":     {"available": true, "breaker_open": false, "cooldown_remaining": 0.0},
  "deepseek": {"available": true, "breaker_open": false, "cooldown_remaining": 0.0}
}}
```

### 第一次调用

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}]}'
```

OpenAI SDK（任何 OpenAI 兼容客户端同理）：

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="<GATEWAY_API_KEY>")
r = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "你好"}],
)
print(r.choices[0].message.content)
```

流式（`stream: true`）、`tools`、`response_format`、`stream_options.include_usage` 等均支持并透传上游；参数支持度详见 [docs/API.md §8.2](docs/API.md#82-openai-参数支持度实测)。

## 可用模型

默认 `config.example.yaml` 注册了三个模型名（交大优先 + DeepSeek 官方兜底）：

| 模型名 | 交大侧 | 上下文窗口 |
|---|---|---|
| `deepseek-chat` | DeepSeek V4 Flash | 256K |
| `deepseek-reasoner` | 思考模式（`reasoning_content`） | 256K |
| `qwen` | Qwen3.6-27B 多模态（base64 图片 ✅） | 256K |

交大还提供 `minimax` / `minimax-m2.7`（192K，原生思考）等，在 `config.yaml` 的 sjtu 供应商 `models` 列表中追加即可，无需改代码：

```yaml
providers:
  - name: sjtu
    models: [deepseek-chat, deepseek-reasoner, minimax, minimax-m2.7, qwen, qwen3.6-27b]
```

## 配置说明

### `.env`（密钥）

| 变量 | 用途 | 默认 |
|---|---|---|
| `SJTU_API_KEY` | 交大密钥 | 必填 |
| `VPN_USERNAME` / `VPN_PASSWORD` | jAccount（vpn 容器） | 必填 |
| `DEEPSEEK_API_KEY` | 备用 DeepSeek 官方 | 未设则该供应商不可用 |
| `GATEWAY_API_KEY` | 网关鉴权；**空 = 不鉴权**（仅本机可达） | 空 |

### `config.yaml`（拓扑）

完整字段见 [docs/API.md §6](docs/API.md#6-配置参考)。要点：

- `providers` 按 `priority` 排序构成每个模型的候选链（越小越优先，交大永远第一）；追加任意 OpenAI 兼容上游只需加一段
- 上游模型名不同时用 `model_map` 映射，如 `model_map: { minimax: "上游实际模型名" }`（同时把该模型注册进目录）
- **⚠️ 非 sjtu 供应商务必显式声明 `context_window`**——缺省会兜到交大内置表导致误路由（deepseek/qwen 系 262144、minimax 系 196608）
- `proactive_rate_limit` 主动限速（令牌桶），默认只对交大配置（9 次/分）
- VPN 相关（`VPN_SERVER` 等）有合理默认，一般不用动

## 日常运维

```bash
docker compose up -d --build        # 更新代码后重启（必须 --build）
docker compose logs -f vpn gateway  # 结构化日志：model=/provider=/status=/switch_trace=
docker compose logs -f vpn          # 隧道协商/路由（TS、VIP、table 220）
curl http://127.0.0.1:8000/stats    # 每供应商累计计数（切换/限速/熔断）
```

常见问题：

| 症状 | 处置 |
|---|---|
| 改了代码行为没变 | 镜像没重建——`docker compose up -d --build` |
| vpn 一直 unhealthy | `docker compose logs vpn` 看协商日志；jAccount 会话被锁最长约 8 分钟自愈 |
| 构建 vpn 镜像时 apt 卡死 | `vpn/Dockerfile` 使用交大镜像源（mirror.sjtu.edu.cn），不可达时改回 `deb.debian.org` 或换源 |
| 502 `all_providers_failed` | `/stats` 看 `rate_limited`（等 60s 冷却）或 `network_errors`（查 vpn 日志） |
| 413 `context_length_exceeded` | 请求确实超出所有候选窗口：裁剪输入或给该模型配更大窗口的兜底供应商 |
| 容器起不来 exec 报 no such file | Windows 检出 CRLF——仓库 `.gitattributes` 已强制 LF，重新检出：`git rm --cached -r . && git reset --hard` |
| 疑似流量未走隧道 | `docker compose exec vpn ip xfrm policy` / `ip route show table 220` |

## 开发与测试

```bash
cd gateway && uv run pytest    # 146 个单元/集成测试（估算/路由/归一/熔断/SSRF/鉴权等）
```

模块划分（`gateway/app/`）：`main`（端点/鉴权）→ `forward`（转发引擎/宽切换）→ `providers`（候选链）→ `estimate`（token 估算）→ `config`（配置/窗口表）→ `normalize`（归一 + 流式状态机）→ `failover`（错误分类/熔断）→ `ratelimit`（令牌桶）→ `stats` / `security`。VPN 侧在 `vpn/`（Dockerfile + swanctl 模板 + 自愈 entrypoint）。

```text
.
├── docker-compose.yml     # 编排：vpn（发布 127.0.0.1:8000）+ gateway（共享其 netns）
├── config.example.yaml    # 网关拓扑示例（复制为 config.yaml）
├── .env.example           # 密钥模板（复制为 .env）
├── gateway/               # FastAPI 网关（Python 3.13）
├── vpn/                   # strongSwan IKEv2 隧道容器
└── docs/                  # API.md 完整文档 + 设计文档
```

## 文档

- [docs/API.md](docs/API.md) — 完整文档：端点参考、聚合语义、故障切换矩阵、配置全字段、上游实测、运维排障
- 设计文档：[v1 网关设计](docs/superpowers/specs/2026-08-31-sjtu-llm-gateway-design.md) · [v2 聚合层设计](docs/superpowers/specs/2026-09-06-aggregation-layer-design.md) · [VPN spike 结论](docs/superpowers/notes/2026-08-31-vpn-spike.md)
- 交大 API 官方文档：<https://claw.sjtu.edu.cn/guide/sjtu-api/>（支持：hpc@sjtu.edu.cn）
