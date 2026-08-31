# SJTU LLM API 转发平台设计文档

- 日期：2026-08-31
- 状态：已获用户批准的设计（不含 Anthropic `/v1/messages` 入口）
- 技术栈：Python 3.13 + FastAPI + httpx；Docker Compose 部署；strongSwan IKEv2

## 1. 背景与目标

上海交大网络信息中心向师生提供免费 LLM API（`https://models.sjtu.edu.cn/api/v1`，OpenAI 兼容），但有两个限制：

1. **仅限校园网访问**——校外需 VPN，且用户要求 VPN 由平台自身在应用内建立维护，不依赖宿主机连接；
2. **速率限制紧**——每分钟 10 次请求、30 万 token/分钟，本机多个应用共用时 429 频发。

本平台是一个运行在本机（WSL2 + Docker）的 API 转发网关：

- 对本机任意应用暴露一个本地 OpenAI 兼容入口；
- 平台内部维护到交大的 IKEv2 VPN 隧道，且**只有发往交大模型 API 的流量走隧道**，其余流量（备用 API）直连；
- 当交大 API 因限额、速率限制、故障不可用时，**自动切换**到可配置的备用 OpenAI 兼容服务（如 DeepSeek 官方），恢复后自动切回（省钱优先）。

## 2. 需求

### 2.1 功能需求

- FR1 提供 OpenAI 兼容端点：`POST /v1/chat/completions`、`GET /v1/models`，支持流式（SSE）与非流式。
- FR2 应用内 VPN：容器内 strongSwan 以 IKEv2 连接 `stu.vpn.sjtu.edu.cn`（学生网关），jAccount 用户名/密码认证；断线自动重连。
- FR3 精确分流：仅交大网段（`202.120.0.0/16` 及 `models.sjtu.edu.cn` 解析 IP）走隧道；备用供应商流量直连；不影响宿主机路由。
- FR4 供应商链可配置：任意数量 OpenAI 兼容上游（base_url、密钥环境变量名、可服务模型列表、优先级、模型名映射）。
- FR5 宽切换故障处理：429、配额类错误、5xx、连接失败/超时、流中断 → 自动漫游到下一供应商，配合熔断冷却。
- FR6 对交大上游做主动限速（默认 9 次/分钟令牌桶），排队超时（默认 2s）直接走备用。
- FR7 可选本地鉴权：`GATEWAY_API_KEY` 非空时要求客户端 Bearer 鉴权；本机可达性由 Compose 端口映射 `127.0.0.1:8000:8000` 保证（仅宿主机回环可访问）。
- FR8 运维端点：`GET /health`（隧道状态 + 各供应商可达性）、`GET /stats`（各供应商请求/错误/切换/冷却统计）。

### 2.2 非目标

- 不做 Anthropic `/v1/messages` 入口（用户明确砍掉；交大原生支持，将来需要时可加）。
- 不做 Web 管理界面。
- 不做多用户/多租户（单用户本机使用）。
- 不做 embeddings（交大未提供）。
- 不做计费，仅做请求统计。

## 3. 架构

```
本机应用（OpenAI SDK / 任意工具）
   │  http://127.0.0.1:8000/v1   （Bearer 本地密钥，可配置关闭）
   ▼
┌─ gateway 容器（FastAPI）─────────────────────────────┐
│  入口: /v1/chat/completions  /v1/models  /health /stats │
│  路由: 按模型名找候选供应商 → 跳过熔断中的 → 逐个尝试      │
│  供应商: sjtu(P1) → deepseek(P2) → 其它兼容API(P3+)      │
└──────────────┬──────────────────────────────────────────┘
               │ 共享网络命名空间 (network_mode: service:vpn)
┌─ vpn 容器（strongSwan）──────────────────────────────┐
│  IKEv2 → stu.vpn.sjtu.edu.cn（EAP，jAccount 凭据）     │
│  仅 202.120.0.0/16 + 模型API IP 走隧道，其余直连         │
│  健康探测 + 断线自动重连                                  │
└───────────────────────────────────────────────────────┘
```

关键决策：两个容器共享一个网络命名空间（`network_mode: "service:vpn"`）。网关进程无需感知 VPN——发往交大 IP 的流量按容器内路由规则进隧道，其余走默认路由直连。VPN 完全封装在容器 netns 内，不碰宿主机。

`vpn` 服务对外发布网关端口：`127.0.0.1:8000 -> gateway:8000`。

## 4. 组件设计

### 4.1 入口层（`app/main.py`）

- `POST /v1/chat/completions`：透传请求体（不解析/不改写字段，除 Authorization 与 model 映射）；`stream=true` 时 SSE 逐块透传。
- `GET /v1/models`：返回候选供应商中优先级最高且可用者的实时模型列表（交大文档要求用 `/models` 返回的 `id` 字段作为调用名）；不可用时返回配置文件中的静态列表。
- 鉴权中间件：`GATEWAY_API_KEY` 非空时校验 `Authorization: Bearer <key>`，失败返回本地 401，不转发。
- 响应头注入 `X-Gateway-Provider: <name>` 便于客户端与日志排查实际服务方。

### 4.2 配置（`app/config.py` + `config.yaml`）

```yaml
listen_host: 0.0.0.0        # 容器内必须 0.0.0.0（端口映射才能到达）；宿主机侧仅暴露 127.0.0.1
listen_port: 8000
providers:
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY          # 密钥只从环境变量读，不落盘
    models: [deepseek-chat, deepseek-reasoner, minimax, minimax-m2.7, qwen, qwen3.6-27b]
    priority: 1
    proactive_rate_limit: { requests_per_minute: 9, max_wait_seconds: 2 }
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models: [deepseek-chat, deepseek-reasoner]
    priority: 2
  - name: 其它任意 OpenAI 兼容服务        # 硅基流动/OpenRouter/自建等
    base_url: https://example.com/v1
    api_key_env: EXAMPLE_API_KEY
    model_map: { minimax: "上游实际模型名", qwen: "上游实际模型名" }  # 名称不同时映射；同名可省略
    priority: 3
failover:
  cooldown_429_seconds: 60
  cooldown_quota_seconds: 1800
  cooldown_network_seconds: 15
  connect_timeout_seconds: 10
  read_timeout_seconds: 600            # 交大文档建议长推理超时
```

加载时校验：必填项齐全；`api_key_env` 指向的环境变量存在（缺失则该供应商标记为不可用并告警，不阻断启动）；`base_url` 通过安全校验（见 4.6）。

### 4.3 供应商选择（`app/providers.py`）

- 请求模型名 → 过滤出 `models` 列表或 `model_map` 键包含该模型的供应商 → 按 `priority` 升序排列 → 剔除熔断冷却中的 → 得到候选链。
- 候选链为空时返回 404 模型不存在（本地生成，OpenAI 错误格式）。
- `model_map` 命中时把请求中的模型名替换为映射后的上游名。

### 4.4 Failover 引擎（`app/failover.py` + `app/ratelimit.py`）

错误分类与处置矩阵：

| 上游情况 | 处置 | 熔断 |
|---|---|---|
| HTTP 429 | 换下一候选 | 60s（可配） |
| HTTP 402 / 响应体含配额耗尽特征 | 换下一候选 | 30min（可配） |
| HTTP 其它 4xx（400/404/422 等请求问题） | **不切换**，透传给客户端 | 无 |
| HTTP 5xx | 换下一候选 | 15s（可配） |
| 连接失败 / TLS 错误 / 超时 | 换下一候选；VPN 侧并行自愈 | 15s（可配） |
| 流式响应已开始后中断 | **不重试**，透传错误 | 15s |

规则：

- **重试安全**：仅当尚未向客户端写出任何字节时才换供应商整体重试；一旦流已开始（首字节已发出），中断只透传错误，绝不重复输出。
- **熔断恢复**：冷却到期后半开——放一个探测请求，成功则恢复，失败则重新冷却。
- **主动限速**：仅对配置了 `proactive_rate_limit` 的供应商（默认交大）生效。令牌桶按每分钟 N 次补充；请求需先取令牌，等待超过 `max_wait_seconds` 仍无令牌则视为 429 同类信号跳到下一候选（并记录该供应商"软饱和"计数）。
- **省钱优先**：交大 priority 永远最小，只要它可用就优先使用；切换只在它暂时不可用时发生。

### 4.5 VPN 容器（`vpn/`）

- 镜像：Debian/Alpine 基础 + strongSwan（swanctl 配置风格）+ curl（健康探测）。
- 环境变量：`VPN_SERVER=stu.vpn.sjtu.edu.cn`、`VPN_USERNAME`、`VPN_PASSWORD`（jAccount，经 compose `.env` 注入）、`VPN_ROUTE_SUBNETS=202.120.0.0/16`、`VPN_EXTRA_HOSTS=models.sjtu.edu.cn`。
- 连接建立：swanctl 发起 IKEv2 + EAP 认证；**忽略服务器推送的默认路由（0.0.0.0/0）**，只对 `VPN_ROUTE_SUBNETS` 与 `VPN_EXTRA_HOSTS` 解析出的 IP 安装走隧道的路由。
- 自愈：entrypoint 循环——隧道健康探测（`curl -m 5 -s -o /dev/null -w '%{http_code}' https://models.sjtu.edu.cn/api/v1/models`，任意 HTTP 状态码即视为网络可达）失败则重新发起连接；DPD 探测由 strongSwan 负责。
- Compose：`cap_add: [NET_ADMIN]`、`/dev/net/tun` 设备、`restart: unless-stopped`、健康检查即上述探测。
- 注意：健康探测同时是网关判断 `sjtu` 供应商网络可达性的信号来源之一（网关自身在共享 netns 内探测结果一致）。

### 4.6 安全（`app/security.py`）

- **上游地址校验**（配置加载时执行一次 + 转发时按解析结果复核）：
  - scheme 仅允许 `http`/`https`；
  - 解析目标 host 的全部 A/AAAA 记录，拒绝环回（127.0.0.0/8、::1）、私有（10/8、172.16/12、192.168/16、fc00::/7 等）、链路本地、组播及各类保留地址；校验失败拒绝该供应商并告警。
- API 密钥只从环境变量读取；请求日志与错误日志脱敏（不记录任何 `Authorization` 值与密钥环境变量值）。
- 交大 API key 严禁写入仓库/配置/日志（学校规定，违者取消资格）。

### 4.7 可观测（`app/stats.py`）

- 内存计数器：每供应商请求数、成功数、429 数、5xx 数、网络错误数、切换发生次数、软饱和次数、当前熔断状态与剩余冷却时间。
- `GET /stats` 返回上述 JSON；`GET /health` 返回隧道探测结果 + 各供应商最近一次探测结果。
- 结构化日志（一行一条，含时间、模型、供应商链、最终供应商、耗时、切换原因、上游状态码）。

## 5. 数据流示例

```
应用 → POST /v1/chat/completions {model: "deepseek-chat", stream: true}
  1. 鉴权（若启用）
  2. 候选链 = [sjtu, deepseek]（按 priority）
  3. sjtu 熔断中？否则取令牌桶令牌（等待 ≤2s）
  4. 转发至 https://models.sjtu.edu.cn/api/v1/chat/completions（netns 内经隧道）
  5. 收到 429 → 记录熔断 60s → 取下一候选
  6. 转发至 https://api.deepseek.com/v1/chat/completions（直连）
  7. 200 → SSE 透传回应用，响应头 X-Gateway-Provider: deepseek
  8. 日志：model=deepseek-chat chain=sjtu→deepseek reason=429 provider=deepseek
```

## 6. 目录结构

```
SJTU-LLMAPI/
├── AGENTS.md
├── .gitignore                # 忽略 .env、config.yaml（含真实配置）、个人通知 txt、.mimosa/
├── docker-compose.yml
├── config.example.yaml
├── .env.example              # SJTU_API_KEY= / DEEPSEEK_API_KEY= / VPN_USERNAME= / VPN_PASSWORD= / GATEWAY_API_KEY=
├── docs/superpowers/specs/
├── gateway/
│   ├── pyproject.toml        # uv 管理；fastapi、uvicorn、httpx、pyyaml、pytest、respx
│   ├── app/
│   │   ├── main.py           # FastAPI 入口与路由
│   │   ├── config.py
│   │   ├── providers.py
│   │   ├── failover.py
│   │   ├── ratelimit.py
│   │   ├── forward.py        # 转发与 SSE 透传
│   │   ├── security.py
│   │   └── stats.py
│   └── tests/
└── vpn/
    ├── Dockerfile
    ├── swanctl.conf.template
    └── entrypoint.sh
```

## 7. 测试策略

- **单元（TDD，pytest）**：候选链构造与 priority 排序；model_map 映射；熔断状态机（进入冷却/半开/恢复/重新冷却）；4xx 不切换与 429/5xx/网络错误切换的分类；流已开始不重试；令牌桶补充与 max_wait 行为；SSRF 校验（环回/私有/保留/非 http 协议全部拒绝）；配置校验。
- **集成**：本地 mock 上游（测试内置小型 ASGI 服务，可脚本化返回 429、超时挂起、流中途断开、正常 SSE）驱动完整入口 → 验证切换、透传、统计。
- **Spike（风险前置）**：真实 strongSwan ↔ `stu.vpn.sjtu.edu.cn` 连通性验证（EAP 方式与加密提案无公开文档），失败则按第 8 节降级。
- **端到端**：compose 起全栈，真实调用交大 `deepseek-chat` 一轮。

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 学校 IKEv2 EAP 细节未知（认证方式/加密提案） | strongSwan 连不上 | 实现计划第一步即真实 spike；失败则 `vpn/` 容器整体替换为 docker-easyconnect 镜像（SOCKS5 方式，网关加 `route: socks5` 支持），架构其余部分不变 |
| jAccount 密码含特殊字符/需双因素 | 认证失败 | spike 中验证；EasyConnect 方案亦为后备 |
| 交大改 IP 或换域名 | 隧道路由漏掉新 IP | entrypoint 周期性重新解析 `models.sjtu.edu.cn` 并更新路由 |
| 长推理（deepseek-reasoner）超过 read timeout | 流中断 | 默认 read timeout 600s，透传期间不计入（以首字节为界分段计时） |
| 周限额耗尽信号形态未知 | 误判为普通 429 冷却过短 | 按响应体关键词识别配额类错误；无法识别时按 429 处理（60s 冷却，代价可接受） |

## 9. 验收标准

1. 校园网/隧道两种网络环境下，本机应用配置 `base_url=http://127.0.0.1:8000/v1` + 交大模型名即可正常对话（流式与非流式）。
2. 人为触发 429（连续快速请求）后，后续请求在冷却期内自动由备用供应商服务，客户端无感知；交大冷却结束后自动切回。
3. 交大容器内隧道断开时：发往交大的流量失败自动漫游，VPN 容器自动重连。
4. 配置文件中新增任意 OpenAI 兼容上游无需改代码。
5. `git` 仓库中无任何 API 密钥或 jAccount 凭据；日志中无密钥明文。
6. 单元与集成测试全部通过。
