# AGENTS.md

## What this workspace is

Notes and scratch space for working with the SJTU (Shanghai Jiao Tong University) LLM Model Service API. It is **not** a git repository and contains no code yet — expect to create scripts/projects here that call this API.

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

## Build / test

None yet — no package manager, linter, or test framework is set up. When the first project is added, record its commands here.
