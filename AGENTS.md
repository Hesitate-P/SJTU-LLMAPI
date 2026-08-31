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

## 项目：sjtu-llm-gateway（本地 LLM API 转发网关）

OpenAI 兼容本地网关（`gateway/`，Python 3.13 + FastAPI）+ 应用内 strongSwan IKEv2 隧道容器（`vpn/`）+ Compose 编排（共享网络命名空间，宿主仅 `127.0.0.1:8000`）。交大 API 优先，429/配额/5xx/网络错误自动切换到可配置的备用供应商（`config.yaml`）。

- 设计文档：`docs/superpowers/specs/2026-08-31-sjtu-llm-gateway-design.md`；实现计划：`docs/superpowers/plans/2026-08-31-sjtu-llm-gateway.md`
- 网关测试：`cd gateway && uv run pytest`
- 全栈启动：`cp .env.example .env && cp config.example.yaml config.yaml`（填密钥）→ `docker compose up -d`；排障 `docker compose logs -f vpn gateway`；`curl http://127.0.0.1:8000/health`（免鉴权）
- **验收状态**：Task 13（真实 IKEv2 spike，已通过，见 `docs/superpowers/notes/2026-08-31-vpn-spike.md`）与 Task 14（端到端验收，已通过：非流式/流式/限速切换/日志脱敏/Anthropic 入口 404 全达标）均完成；根日志 `logging.basicConfig` 修复已落在 `gateway/app/main.py`（生产部署下 gateway 的 INFO 请求日志可输出）
- 已知合并后积压项：见 `.superpowers/sdd/2026-08-31-sjtu-llm-gateway/progress.md` 的 minor triage（若该目录已删，见 git 历史最终审查条目）
