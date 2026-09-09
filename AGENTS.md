# AGENTS.md

## What this workspace is

Notes and scratch space for working with the SJTU (Shanghai Jiao Tong University) LLM Model Service API. Now a git repository hosting the `sjtu-llm-gateway` project (see bottom section); the reference files below predate it.

Reference files:

- `模型服务API信息通知.txt` — the API approval notification (primary source of truth for API details).
- `新建 文本文档.txt` — largely a duplicate of the same notification; treat the first file as canonical.
- `sjtu vpn.txt` — link to SJTU network/VPN instructions (https://net.sjtu.edu.cn/info/1200/3286.htm), relevant because the API requires campus network access.
- `.mimosa/` — plugin hook state, not project content; ignore it.

## SJTU Model API essentials

- Base URL: `https://models.sjtu.edu.cn/api/v1` (OpenAI-compatible style per the docs).
- Model calling names: `deepseek-chat`, `deepseek-reasoner` (both DeepSeek V4 Flash), `minimax` / `minimax-m2.7` (MiniMax-M2.7), `qwen` / `qwen3.6-27b` (Qwen3.6-27B).
- Rate limits: 10 requests/minute, 300,000 tokens/minute, 1,000,000,000 tokens/week. Any code written here must respect these (e.g., retry with backoff on 429).
- **Campus network required** — the API only works from the SJTU network; off-campus use needs the VPN (see `sjtu vpn.txt`). Failures to connect are often network-related, not code bugs.
- Official docs: https://claw.sjtu.edu.cn/guide/sjtu-api/
- Support: hpc@sjtu.edu.cn (network information center).

## Rules for any code created here

- The API key is **masked** in the notification files; the real key lives with the user. Never hardcode a key — read it from an environment variable (e.g., `SJTU_API_KEY`) or an untracked secrets file. Never print or echo key values into files, logs, or this AGENTS.md.
- Files use Chinese names; keep filenames and content UTF-8 safe in any tooling.

## 项目：sjtu-llm-gateway（本地 LLM 聚合网关）

OpenAI 兼容本地**聚合**网关（`gateway/`，Python 3.13 + FastAPI）+ 应用内 strongSwan IKEv2 隧道容器（`vpn/`）+ Compose 编排（共享网络命名空间，宿主仅 `127.0.0.1:8000`）。交大 API 优先；上下文窗口感知路由（413 保护）；429/配额/5xx/网络错误自动切换到可配置备用供应商；响应协议归一（model 回写、`<think>`→`reasoning_content`，流式含状态机）；`/v1/models` 聚合目录。

- **完整文档：`docs/API.md`**（架构/端点/聚合语义/故障切换/配置/上游实测/运维/测试）
- 设计：v1 `docs/superpowers/specs/2026-08-31-sjtu-llm-gateway-design.md`、v2 聚合层 `2026-09-06-aggregation-layer-design.md`
- 网关测试：`cd gateway && uv run pytest`（146 个）
- 全栈：`cp .env.example .env && cp config.example.yaml config.yaml`（填密钥）→ `docker compose up -d --build`（**代码更新必须 --build**）；排障 `docker compose logs -f vpn gateway`；`curl http://127.0.0.1:8000/health`（免鉴权）
- 验收：v1 端到端（2026-08-31）与 v2 真实端到端 20/20（2026-09-09，上下文路由/归一/聚合全达标）均通过；mimosa 深扫 0 findings
- 本地诊断脚本在 `~/sjtu-probes/`（live_v2_test.py / probe_sjtu.py / probe_openai_params.py，凭据从环境变量读，不入库）
