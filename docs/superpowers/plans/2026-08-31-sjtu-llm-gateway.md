# SJTU LLM API 转发平台实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建本机 OpenAI 兼容网关：应用内 strongSwan IKEv2 隧道访问交大 LLM API，限额/故障时自动切换到可配置的备用 OpenAI 兼容上游。

**Architecture:** 两个容器共享一个网络命名空间（`network_mode: service:vpn`）。vpn 容器跑 strongSwan，IKEv2 流量选择器只圈定交大网段；gateway 容器跑 FastAPI，按模型名构造候选供应商链，宽切换 + 熔断冷却 + 对交大的主动令牌桶限速。

**Tech Stack:** Python 3.13 + FastAPI + httpx + PyYAML（uv 管理）；strongSwan（swanctl）；Docker Compose；pytest + pytest-asyncio。

**Spec:** `docs/superpowers/specs/2026-08-31-sjtu-llm-gateway-design.md`

## Global Constraints

- 上游 URL 仅允许 http/https；host 解析结果拒绝环回/私有/链路本地/组播/保留/未指定地址（配置加载时校验；`resolver` 参数供测试注入）。
- API 密钥与 jAccount 凭据只经环境变量/`.env` 注入：不写入仓库、配置示例的值恒为空、日志不得输出 `Authorization` 值或密钥值。
- 网关容器内监听 `0.0.0.0:8000`；宿主机只经 Compose `127.0.0.1:8000:8000` 暴露。
- VPN 容器 `charon.install_routes = no`，隧道范围只由 CHILD 的 `remote_ts`（`202.120.0.0/16` + `models.sjtu.edu.cn` 解析 IP/32）圈定。
- 依赖只用 fastapi、uvicorn、httpx、pyyaml（运行时）+ pytest、pytest-asyncio（开发）。不引入 respx（用 httpx.MockTransport）。
- 每个任务：先写失败测试 → 实现 → 测试通过 → `git commit`。提交信息用 conventional commits（feat:/test:/build:/docs:/chore:）。
- Python 测试命令统一为 `uv run pytest`（在 `gateway/` 目录下执行）。
- 当前机器可直连 `models.sjtu.edu.cn`（校园网/宿主 VPN）；单元与集成测试一律用 mock 上游，真实调用只在 Task 13/14。

---

### Task 1: 网关脚手架（uv + FastAPI + /health）

**Files:**
- Create: `gateway/pyproject.toml`
- Create: `gateway/app/__init__.py`（空文件）
- Create: `gateway/app/main.py`
- Test: `gateway/tests/test_health.py`

**Interfaces:**
- Produces: `app.main:create_app(cfg: AppConfig | None = None, service: GatewayService | None = None) -> FastAPI`；模块级 `app = create_app()`；`GET /health` 返回 `{"status": "ok"}`（Task 9 扩展）。

- [ ] **Step 1: 初始化项目与依赖**

`gateway/pyproject.toml`：

```toml
[project]
name = "sjtu-llm-gateway"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["fastapi>=0.115", "uvicorn>=0.30", "httpx>=0.27", "pyyaml>=6.0"]

[dependency-groups]
dev = ["pytest>=8", "pytest-asyncio>=0.24"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

```bash
cd gateway && uv sync && uv add --group dev pytest pytest-asyncio
```

- [ ] **Step 2: 写失败测试**

`gateway/tests/test_health.py`：

```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_ok():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 3: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_health.py -v`
Expected: FAIL（`ModuleNotFoundError: app.main` 或类似）

- [ ] **Step 4: 最小实现**

`gateway/app/main.py`：

```python
from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="sjtu-llm-gateway")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
```

- [ ] **Step 5: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_health.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add gateway/
git commit -m "feat: 网关脚手架（FastAPI /health）"
```

---

### Task 2: SSRF 上游地址校验（app/security.py）

**Files:**
- Create: `gateway/app/security.py`
- Test: `gateway/tests/test_security.py`

**Interfaces:**
- Produces: `assert_safe_upstream_url(url: str, resolver: Callable[[str], list[str]] | None = None) -> None`。校验失败抛 `ValueError`；`resolver(host) -> IP 字符串列表` 供测试注入，默认用 `socket.getaddrinfo`。Task 3 的 `load_config` 以 `resolver=` 透传。

- [ ] **Step 1: 写失败测试**

`gateway/tests/test_security.py`：

```python
import pytest

from app.security import assert_safe_upstream_url

PUBLIC = ["93.184.216.34"]


def _res(ips):
    return lambda host: ips


@pytest.mark.parametrize("url", [
    "http://example.com/api", "https://models.sjtu.edu.cn/api/v1",
])
def test_http_https_with_public_host_passes(url):
    assert_safe_upstream_url(url, resolver=_res(PUBLIC))


@pytest.mark.parametrize("url", [
    "ftp://example.com/api", "file:///etc/passwd", "gopher://example.com",
])
def test_non_http_scheme_rejected(url):
    with pytest.raises(ValueError, match="scheme"):
        assert_safe_upstream_url(url, resolver=_res(PUBLIC))


def test_missing_host_rejected():
    with pytest.raises(ValueError, match="host"):
        assert_safe_upstream_url("http:///path", resolver=_res(PUBLIC))


@pytest.mark.parametrize("host_ip", [
    "127.0.0.1", "::1", "10.1.2.3", "172.16.0.9", "192.168.1.1",
    "169.254.1.1", "fd00::1", "224.0.0.1", "0.0.0.0",
])
def test_forbidden_ips_rejected(host_ip):
    with pytest.raises(ValueError, match="环回|私有|保留"):
        assert_safe_upstream_url(f"https://upstream.test/v1", resolver=_res([host_ip]))


def test_literal_loopback_ip_rejected_without_resolver():
    with pytest.raises(ValueError):
        assert_safe_upstream_url("http://127.0.0.1:8080/v1")


def test_unresolvable_host_rejected():
    with pytest.raises(ValueError, match="解析"):
        assert_safe_upstream_url("https://nope.test/v1", resolver=lambda host: [])
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_security.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`gateway/app/security.py`：

```python
"""上游 URL 安全校验：scheme 仅 http/https；host 不得解析为环回/私有/保留地址。"""
from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import urlparse


def _is_forbidden_ip(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return (
        addr.is_loopback
        or addr.is_private
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def assert_safe_upstream_url(
    url: str, resolver: Callable[[str], list[str]] | None = None
) -> None:
    """校验失败抛 ValueError；resolver(host)->IP 列表，默认 socket.getaddrinfo。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"scheme 仅允许 http/https: {url!r}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL 缺少 host: {url!r}")
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        if resolver is not None:
            ips = resolver(host)
        else:
            ips = sorted({info[4][0] for info in socket.getaddrinfo(host, None)})
    if not ips:
        raise ValueError(f"无法解析 host: {host}")
    for ip in ips:
        if _is_forbidden_ip(ip):
            raise ValueError(f"禁止访问环回/私有/保留地址: {host} -> {ip}")
```

- [ ] **Step 4: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_security.py -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add gateway/app/security.py gateway/tests/test_security.py
git commit -m "feat: 上游 URL SSRF 校验"
```

---

### Task 3: 配置加载与校验（app/config.py）

**Files:**
- Create: `gateway/app/config.py`
- Create: `config.example.yaml`（仓库根）
- Test: `gateway/tests/test_config.py`

**Interfaces:**
- Consumes: `assert_safe_upstream_url(url, resolver=)`
- Produces:
  - `@dataclass RateLimitConfig(requests_per_minute: int, max_wait_seconds: float = 2.0, burst: int = 3)`
  - `@dataclass ProviderConfig(name, base_url, api_key_env, priority: int, models: list[str], model_map: dict[str, str], proactive_rate_limit: RateLimitConfig | None, available: bool, unavailable_reason: str)`
  - `@dataclass FailoverConfig(cooldown_429_seconds=60.0, cooldown_quota_seconds=1800.0, cooldown_network_seconds=15.0, connect_timeout_seconds=10.0, read_timeout_seconds=600.0)`
  - `@dataclass AppConfig(listen_host="0.0.0.0", listen_port=8000, providers: list[ProviderConfig], failover: FailoverConfig)`，属性 `gateway_api_key -> str | None`（读环境变量 `GATEWAY_API_KEY`）
  - `load_config(path: str, environ: Mapping[str, str] | None = None, resolver=None) -> AppConfig`（providers 按 priority 升序排好）

- [ ] **Step 1: 写失败测试**

`gateway/tests/test_config.py`：

```python
import pytest

from app.config import load_config

PUBLIC = ["93.184.216.34"]
RES = lambda host: PUBLIC

YAML = """
listen_port: 9000
providers:
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models: [deepseek-chat]
    priority: 2
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY
    models: [deepseek-chat, minimax]
    priority: 1
    proactive_rate_limit:
      requests_per_minute: 9
      max_wait_seconds: 2
failover:
  cooldown_429_seconds: 30
"""


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(YAML, encoding="utf-8")
    return load_config(str(path), environ={"SJTU_API_KEY": "k1", "DEEPSEEK_API_KEY": "k2"}, resolver=RES)


def test_providers_sorted_by_priority(cfg):
    assert [p.name for p in cfg.providers] == ["sjtu", "deepseek"]


def test_fields_parsed(cfg):
    sjtu = cfg.providers[0]
    assert sjtu.base_url == "https://models.sjtu.edu.cn/api/v1"
    assert sjtu.available and sjtu.unavailable_reason == ""
    assert sjtu.proactive_rate_limit.requests_per_minute == 9
    assert cfg.failover.cooldown_429_seconds == 30
    assert cfg.listen_port == 9000


def test_missing_key_env_marks_unavailable(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(YAML, encoding="utf-8")
    cfg = load_config(str(path), environ={"SJTU_API_KEY": "k1"}, resolver=RES)
    deepseek = next(p for p in cfg.providers if p.name == "deepseek")
    assert deepseek.available is False
    assert "DEEPSEEK_API_KEY" in deepseek.unavailable_reason


def test_forbidden_base_url_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n  - name: bad\n    base_url: http://10.0.0.5/v1\n"
        "    api_key_env: X\n    priority: 1\n    models: [m]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_config(str(path), environ={"X": "k"}, resolver=RES)


def test_no_providers_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("providers: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provider"):
        load_config(str(path), environ={}, resolver=RES)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_config.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`gateway/app/config.py`：

```python
"""config.yaml 加载与校验。密钥只从环境变量读，绝不落盘。"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

import yaml

from .security import assert_safe_upstream_url


@dataclass
class RateLimitConfig:
    requests_per_minute: int
    max_wait_seconds: float = 2.0
    burst: int = 3


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key_env: str
    priority: int
    models: list[str] = field(default_factory=list)
    model_map: dict[str, str] = field(default_factory=dict)
    proactive_rate_limit: RateLimitConfig | None = None
    available: bool = True
    unavailable_reason: str = ""


@dataclass
class FailoverConfig:
    cooldown_429_seconds: float = 60.0
    cooldown_quota_seconds: float = 1800.0
    cooldown_network_seconds: float = 15.0
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 600.0


@dataclass
class AppConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 8000
    providers: list[ProviderConfig] = field(default_factory=list)
    failover: FailoverConfig = field(default_factory=FailoverConfig)

    @property
    def gateway_api_key(self) -> str | None:
        return os.environ.get("GATEWAY_API_KEY") or None


def load_config(
    path: str,
    environ: Mapping[str, str] | None = None,
    resolver=None,
) -> AppConfig:
    environ = os.environ if environ is None else environ
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    cfg = AppConfig(
        listen_host=str(raw.get("listen_host", "0.0.0.0")),
        listen_port=int(raw.get("listen_port", 8000)),
    )
    f = raw.get("failover") or {}
    cfg.failover = FailoverConfig(
        cooldown_429_seconds=float(f.get("cooldown_429_seconds", 60.0)),
        cooldown_quota_seconds=float(f.get("cooldown_quota_seconds", 1800.0)),
        cooldown_network_seconds=float(f.get("cooldown_network_seconds", 15.0)),
        connect_timeout_seconds=float(f.get("connect_timeout_seconds", 10.0)),
        read_timeout_seconds=float(f.get("read_timeout_seconds", 600.0)),
    )

    for item in raw.get("providers", []):
        provider = ProviderConfig(
            name=str(item["name"]),
            base_url=str(item["base_url"]).rstrip("/"),
            api_key_env=str(item["api_key_env"]),
            priority=int(item["priority"]),
            models=[str(m) for m in item.get("models", [])],
            model_map={str(k): str(v) for k, v in item.get("model_map", {}).items()},
        )
        rl = item.get("proactive_rate_limit")
        if rl:
            provider.proactive_rate_limit = RateLimitConfig(
                requests_per_minute=int(rl["requests_per_minute"]),
                max_wait_seconds=float(rl.get("max_wait_seconds", 2.0)),
                burst=int(rl.get("burst", 3)),
            )
        assert_safe_upstream_url(provider.base_url, resolver=resolver)
        if not environ.get(provider.api_key_env):
            provider.available = False
            provider.unavailable_reason = f"环境变量 {provider.api_key_env} 未设置"
        cfg.providers.append(provider)

    if not cfg.providers:
        raise ValueError("config.yaml 中没有任何 provider")
    cfg.providers.sort(key=lambda p: p.priority)
    return cfg
```

- [ ] **Step 4: 写示例配置（仓库根 `config.example.yaml`）**

```yaml
listen_host: 0.0.0.0        # 容器内必须 0.0.0.0；宿主机侧仅暴露 127.0.0.1
listen_port: 8000
providers:
  - name: sjtu
    base_url: https://models.sjtu.edu.cn/api/v1
    api_key_env: SJTU_API_KEY
    models: [deepseek-chat, deepseek-reasoner, minimax, minimax-m2.7, qwen, qwen3.6-27b]
    priority: 1
    proactive_rate_limit:
      requests_per_minute: 9
      max_wait_seconds: 2
      burst: 3
  - name: deepseek
    base_url: https://api.deepseek.com/v1
    api_key_env: DEEPSEEK_API_KEY
    models: [deepseek-chat, deepseek-reasoner]
    priority: 2
  # 更多 OpenAI 兼容上游按此格式追加；模型名不同时用 model_map 映射
  # - name: other
  #   base_url: https://example.com/v1
  #   api_key_env: OTHER_API_KEY
  #   model_map: { minimax: "上游实际模型名", qwen: "上游实际模型名" }
  #   priority: 3
failover:
  cooldown_429_seconds: 60
  cooldown_quota_seconds: 1800
  cooldown_network_seconds: 15
  connect_timeout_seconds: 10
  read_timeout_seconds: 600
```

- [ ] **Step 5: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add gateway/app/config.py gateway/tests/test_config.py config.example.yaml
git commit -m "feat: 配置加载与校验 + 示例配置"
```

---

### Task 4: 候选供应商链（app/providers.py）

**Files:**
- Create: `gateway/app/providers.py`
- Test: `gateway/tests/test_providers.py`

**Interfaces:**
- Consumes: `AppConfig`、`ProviderConfig`
- Produces: `build_chain(cfg: AppConfig, model: str) -> list[tuple[ProviderConfig, str]]`——按 priority 升序返回 `(供应商, 上游模型名)`；`model_map` 命中优先且返回映射名；不可用供应商剔除。

- [ ] **Step 1: 写失败测试**

`gateway/tests/test_providers.py`：

```python
from app.config import AppConfig, ProviderConfig
from app.providers import build_chain


def make_cfg():
    return AppConfig(providers=[
        ProviderConfig(name="deepseek", base_url="https://d.test/v1", api_key_env="D",
                       priority=2, models=["deepseek-chat"]),
        ProviderConfig(name="sjtu", base_url="https://s.test/v1", api_key_env="S",
                       priority=1, models=["deepseek-chat", "minimax"]),
        ProviderConfig(name="other", base_url="https://o.test/v1", api_key_env="O",
                       priority=3, model_map={"minimax": "other-mini", "qwen": "other-qwen"}),
        ProviderConfig(name="nokey", base_url="https://n.test/v1", api_key_env="N",
                       priority=4, models=["deepseek-chat"], available=False,
                       unavailable_reason="环境变量 N 未设置"),
    ])


def test_chain_sorted_by_priority():
    chain = build_chain(make_cfg(), "deepseek-chat")
    assert [(p.name, m) for p, m in chain] == [("sjtu", "deepseek-chat"), ("deepseek", "deepseek-chat")]


def test_model_map_translates_name():
    chain = build_chain(make_cfg(), "qwen")
    assert [(p.name, m) for p, m in chain] == [("other", "other-qwen")]


def test_unknown_model_empty_chain():
    assert build_chain(make_cfg(), "no-such-model") == []


def test_unavailable_provider_excluded():
    chain = build_chain(make_cfg(), "deepseek-chat")
    assert all(p.name != "nokey" for p, _ in chain)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_providers.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`gateway/app/providers.py`：

```python
"""请求模型名 -> 候选供应商链（priority 升序）。"""
from __future__ import annotations

from .config import AppConfig, ProviderConfig


def build_chain(cfg: AppConfig, model: str) -> list[tuple[ProviderConfig, str]]:
    chain: list[tuple[ProviderConfig, str]] = []
    for provider in cfg.providers:
        if not provider.available:
            continue
        if model in provider.model_map:
            chain.append((provider, provider.model_map[model]))
        elif model in provider.models:
            chain.append((provider, model))
    return chain
```

- [ ] **Step 4: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_providers.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add gateway/app/providers.py gateway/tests/test_providers.py
git commit -m "feat: 供应商候选链与模型名映射"
```

---

### Task 5: 令牌桶限速（app/ratelimit.py）

**Files:**
- Create: `gateway/app/ratelimit.py`
- Test: `gateway/tests/test_ratelimit.py`

**Interfaces:**
- Produces: `TokenBucket(rate_per_minute: float, burst: float = 3.0)`；`async acquire(max_wait_seconds: float) -> bool`（取到令牌 True；等待超时 False）。连续补充速率 = rate_per_minute/60 每秒。

- [ ] **Step 1: 写失败测试**

`gateway/tests/test_ratelimit.py`：

```python
from app.ratelimit import TokenBucket


async def test_burst_capacity_immediate():
    bucket = TokenBucket(rate_per_minute=9, burst=3)
    assert [await bucket.acquire(0) for _ in range(3)] == [True, True, True]


async def test_exhausted_bucket_fails_fast_with_zero_wait():
    bucket = TokenBucket(rate_per_minute=9, burst=1)
    assert await bucket.acquire(0) is True
    assert await bucket.acquire(0) is False


async def test_waits_for_refill_within_max_wait():
    # 600/分钟 = 10/秒，空桶后 0.1 秒可再取一个
    bucket = TokenBucket(rate_per_minute=600, burst=1)
    assert await bucket.acquire(0) is True
    assert await bucket.acquire(2.0) is True


async def test_times_out_when_refill_too_slow():
    bucket = TokenBucket(rate_per_minute=6, burst=1)  # 0.1/秒
    assert await bucket.acquire(0) is True
    assert await bucket.acquire(0.05) is False
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_ratelimit.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`gateway/app/ratelimit.py`：

```python
"""对上游的主动限速：连续补充令牌桶，排队超时即放弃（上层转走备用）。"""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_minute: float, burst: float = 3.0) -> None:
        self._rate = rate_per_minute / 60.0  # 每秒补充
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
        self._last = now

    async def acquire(self, max_wait_seconds: float) -> bool:
        deadline = time.monotonic() + max_wait_seconds
        async with self._lock:
            while True:
                now = time.monotonic()
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 - self._tokens) / self._rate
                if now + wait > deadline:
                    return False
                await asyncio.sleep(min(wait, deadline - now))
```

- [ ] **Step 4: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_ratelimit.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add gateway/app/ratelimit.py gateway/tests/test_ratelimit.py
git commit -m "feat: 令牌桶主动限速"
```

---

### Task 6: 错误分类与熔断（app/failover.py）

**Files:**
- Create: `gateway/app/failover.py`
- Test: `gateway/tests/test_failover.py`

**Interfaces:**
- Produces:
  - `class ErrorKind(str, Enum): OK / RATE_LIMIT / QUOTA / SERVER / NETWORK / CLIENT`
  - `classify_status(status: int, body_snippet: str = "") -> ErrorKind`（429 且响应体含配额关键词 → QUOTA；402 → QUOTA；5xx → SERVER；4xx → CLIENT；2xx → OK；其它 → SERVER）
  - `Breaker(cooldown_429: float = 60.0, cooldown_quota: float = 1800.0, cooldown_network: float = 15.0)`；`is_open(provider: str, now: float | None = None) -> bool`；`record_failure(provider: str, kind: ErrorKind, now: float | None = None) -> float`（返回冷却截止时刻；CLIENT/OK 不打开）；`cooldown_remaining(provider: str, now: float | None = None) -> float`
  - 冷却到期即放行（半开语义：放行后首个请求失败则重新冷却）。

- [ ] **Step 1: 写失败测试**

`gateway/tests/test_failover.py`：

```python
import pytest

from app.failover import Breaker, ErrorKind, classify_status


@pytest.mark.parametrize("status,expected", [
    (200, ErrorKind.OK), (400, ErrorKind.CLIENT), (404, ErrorKind.CLIENT),
    (429, ErrorKind.RATE_LIMIT), (402, ErrorKind.QUOTA),
    (500, ErrorKind.SERVER), (503, ErrorKind.SERVER),
])
def test_classify_by_status(status, expected):
    assert classify_status(status) is expected


@pytest.mark.parametrize("body", ["quota exceeded", "insufficient balance", "额度已用尽", "配额不足"])
def test_429_with_quota_body_is_quota(body):
    assert classify_status(429, body) is ErrorKind.QUOTA


def test_breaker_opens_and_expires():
    breaker = Breaker(cooldown_429=60.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=100.0)
    assert breaker.is_open("sjtu", now=100.0) is True
    assert breaker.is_open("sjtu", now=159.9) is True
    assert breaker.is_open("sjtu", now=160.1) is False  # 到期放行（半开）


def test_cooldown_durations_by_kind():
    breaker = Breaker(cooldown_429=60.0, cooldown_quota=1800.0, cooldown_network=15.0)
    assert breaker.record_failure("a", ErrorKind.RATE_LIMIT, now=0) == 60
    assert breaker.record_failure("b", ErrorKind.QUOTA, now=0) == 1800
    assert breaker.record_failure("c", ErrorKind.NETWORK, now=0) == 15
    assert breaker.record_failure("d", ErrorKind.SERVER, now=0) == 15


def test_client_error_does_not_open():
    breaker = Breaker()
    breaker.record_failure("sjtu", ErrorKind.CLIENT, now=0)
    assert breaker.is_open("sjtu", now=0) is False


def test_half_open_failure_reopens():
    breaker = Breaker(cooldown_429=10.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=0)
    assert breaker.is_open("sjtu", now=11) is False
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=11)
    assert breaker.is_open("sjtu", now=20) is True


def test_remaining():
    breaker = Breaker(cooldown_429=60.0)
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=100.0)
    assert breaker.cooldown_remaining("sjtu", now=130.0) == pytest.approx(30.0)
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_failover.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`gateway/app/failover.py`：

```python
"""上游错误分类与按供应商熔断。冷却到期自动放行（半开），失败再冷却。"""
from __future__ import annotations

import enum
import time

_QUOTA_KEYWORDS = ("quota", "insufficient", "balance", "exhausted", "额度", "配额")


class ErrorKind(str, enum.Enum):
    OK = "ok"
    RATE_LIMIT = "rate_limit"   # 429：短期速率限制
    QUOTA = "quota"             # 配额耗尽：冷却更久
    SERVER = "server"           # 5xx
    NETWORK = "network"         # 连接失败/超时/流中断（由调用方归类）
    CLIENT = "client"           # 请求本身的问题：透传，不切换


def classify_status(status: int, body_snippet: str = "") -> ErrorKind:
    if status == 429:
        low = body_snippet.lower()
        if any(k in low for k in _QUOTA_KEYWORDS):
            return ErrorKind.QUOTA
        return ErrorKind.RATE_LIMIT
    if status == 402:
        return ErrorKind.QUOTA
    if 500 <= status <= 599:
        return ErrorKind.SERVER
    if 400 <= status <= 499:
        return ErrorKind.CLIENT
    if 200 <= status <= 299:
        return ErrorKind.OK
    return ErrorKind.SERVER


class Breaker:
    def __init__(
        self,
        cooldown_429: float = 60.0,
        cooldown_quota: float = 1800.0,
        cooldown_network: float = 15.0,
    ) -> None:
        self._cooldowns = {
            ErrorKind.RATE_LIMIT: cooldown_429,
            ErrorKind.QUOTA: cooldown_quota,
            ErrorKind.SERVER: cooldown_network,
            ErrorKind.NETWORK: cooldown_network,
        }
        self._until: dict[str, float] = {}

    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def is_open(self, provider: str, now: float | None = None) -> bool:
        return self._until.get(provider, 0.0) > self._now(now)

    def cooldown_remaining(self, provider: str, now: float | None = None) -> float:
        return max(0.0, self._until.get(provider, 0.0) - self._now(now))

    def record_failure(self, provider: str, kind: ErrorKind, now: float | None = None) -> float:
        cooldown = self._cooldowns.get(kind, 0.0)
        until = self._now(now) + cooldown
        if until > self._until.get(provider, 0.0):
            self._until[provider] = until
        return until
```

- [ ] **Step 4: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_failover.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add gateway/app/failover.py gateway/tests/test_failover.py
git commit -m "feat: 错误分类与熔断状态机"
```

---

### Task 7: 统计（app/stats.py）

**Files:**
- Create: `gateway/app/stats.py`
- Test: `gateway/tests/test_stats.py`

**Interfaces:**
- Consumes: `ErrorKind`
- Produces: `Stats()`；`record(provider: str, kind: ErrorKind) -> None`（requests++ 并按类计数：OK→success；RATE_LIMIT→rate_limited；QUOTA→quota；SERVER→server_errors；NETWORK→network_errors）；`note_switched_away(provider)`；`note_soft_saturation(provider)`；`snapshot() -> dict`（每供应商：requests/success/rate_limited/quota/server_errors/network_errors/switched_away/soft_saturated）。单事件循环内使用，无需锁。

- [ ] **Step 1: 写失败测试**

`gateway/tests/test_stats.py`：

```python
from app.failover import ErrorKind
from app.stats import Stats


def test_record_and_snapshot():
    stats = Stats()
    stats.record("sjtu", ErrorKind.OK)
    stats.record("sjtu", ErrorKind.RATE_LIMIT)
    stats.record("sjtu", ErrorKind.RATE_LIMIT)
    stats.record("deepseek", ErrorKind.OK)
    stats.note_switched_away("sjtu")
    stats.note_soft_saturation("sjtu")
    snap = stats.snapshot()
    assert snap["sjtu"]["requests"] == 3
    assert snap["sjtu"]["success"] == 1
    assert snap["sjtu"]["rate_limited"] == 2
    assert snap["sjtu"]["switched_away"] == 1
    assert snap["sjtu"]["soft_saturated"] == 1
    assert snap["deepseek"]["success"] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_stats.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`gateway/app/stats.py`：

```python
"""内存请求统计（单事件循环，无需锁）。"""
from __future__ import annotations

from .failover import ErrorKind

_FIELDS = ("requests", "success", "rate_limited", "quota", "server_errors",
           "network_errors", "switched_away", "soft_saturated")


class Stats:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, int]] = {}

    def _bucket(self, provider: str) -> dict[str, int]:
        return self._data.setdefault(provider, {f: 0 for f in _FIELDS})

    def record(self, provider: str, kind: ErrorKind) -> None:
        b = self._bucket(provider)
        b["requests"] += 1
        counter = {
            ErrorKind.OK: "success",
            ErrorKind.RATE_LIMIT: "rate_limited",
            ErrorKind.QUOTA: "quota",
            ErrorKind.SERVER: "server_errors",
            ErrorKind.NETWORK: "network_errors",
        }.get(kind)
        if counter:
            b[counter] += 1

    def note_switched_away(self, provider: str) -> None:
        self._bucket(provider)["switched_away"] += 1

    def note_soft_saturation(self, provider: str) -> None:
        self._bucket(provider)["soft_saturated"] += 1

    def snapshot(self) -> dict:
        return {name: dict(b) for name, b in self._data.items()}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_stats.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add gateway/app/stats.py gateway/tests/test_stats.py
git commit -m "feat: 供应商请求统计"
```

---

### Task 8: 转发引擎（app/forward.py）——核心

**Files:**
- Create: `gateway/app/forward.py`
- Test: `gateway/tests/test_forward.py`

**Interfaces:**
- Consumes: `AppConfig/ProviderConfig`、`build_chain`、`Breaker/record_failure/is_open`、`classify_status/ErrorKind`、`TokenBucket`、`Stats`
- Produces:
  - `@dataclass GatewayResponse(status_code: int, media_type: str, provider: str, body: bytes | None = None, stream: AsyncIterator[bytes] | None = None)`；属性 `snippet -> str`（body 前 512 字节安全解码，供分类）
  - `GatewayService(cfg, breaker, stats, buckets: dict[str, TokenBucket], client_factory=None)`：
    - `async chat(request_body: dict) -> GatewayResponse`：候选链逐个尝试（跳过熔断中的；令牌桶超时→软饱和并跳过）；网络异常→NETWORK 熔断换下家；CLIENT 透传不切换；OK 返回；全部失败返回最后一个失败响应或本地 502（OpenAI 错误格式，`code="all_providers_failed"`）。缺少 model → 本地 400；无候选 → 本地 404 `model_not_found`。
    - `async list_models() -> GatewayResponse`：首个可用供应商 `GET {base}/models` 实时列表；全失败回退配置静态并集，provider 标 `"config"`。
    - `client_factory: () -> httpx.AsyncClient`（测试用 `httpx.AsyncClient(transport=httpx.MockTransport(handler))` 注入）
  - 流式提交规则：仅在拿到**首块**后才构造 `GatewayResponse(stream=...)`；首块前的任何异常由 `chat` 捕获换家；首块后中断由消费方承接（不重试）。

- [ ] **Step 1: 写失败测试**

`gateway/tests/test_forward.py`：

```python
import json

import httpx
import pytest

from app.config import AppConfig, ProviderConfig
from app.failover import Breaker, ErrorKind
from app.forward import GatewayService
from app.stats import Stats

PUBLIC = ["93.184.216.34"]
RES = lambda host: PUBLIC
CALLS: list[str] = []


def make_cfg():
    return AppConfig(providers=[
        ProviderConfig(name="sjtu", base_url="https://sjtu.test/v1", api_key_env="SJTU_API_KEY",
                       priority=1, models=["m1"]),
        ProviderConfig(name="deepseek", base_url="https://deepseek.test/v1",
                       api_key_env="DEEPSEEK_API_KEY", priority=2, models=["m1"]),
    ])


def make_service(handler, cfg=None):
    cfg = cfg or make_cfg()
    return GatewayService(
        cfg, Breaker(), Stats(), {},
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)),
    )


def ok_sse(*chunks):
    async def gen():
        for c in chunks:
            yield c
    return gen()


async def test_429_fails_over_to_next_provider(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sjtu.test":
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"id": "x", "choices": []})

    result = await make_service(handler).chat({"model": "m1"})
    assert result.provider == "deepseek"
    assert result.status_code == 200
    assert json.loads(result.body)["id"] == "x"


async def test_client_error_passes_through_without_failover(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    result = await make_service(handler).chat({"model": "m1"})
    assert result.status_code == 400
    assert result.provider == "sjtu"
    assert calls == ["sjtu.test"]  # 没有切到第二家


async def test_network_error_fails_over(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sjtu.test":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"ok": True})

    result = await make_service(handler).chat({"model": "m1"})
    assert result.provider == "deepseek"


async def test_all_fail_returns_local_502(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "no"})

    result = await make_service(handler).chat({"model": "m1"})
    assert result.status_code == 502
    assert result.provider == "local"
    assert b"all_providers_failed" in result.body


async def test_breaker_open_skips_provider(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    breaker = Breaker()
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sjtu.test":
            raise AssertionError("熔断中的供应商不应被请求")
        return httpx.Response(200, json={"ok": True})

    cfg = make_cfg()
    service = GatewayService(cfg, breaker, Stats(), {},
                             client_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(handler)))
    result = await service.chat({"model": "m1"})
    assert result.provider == "deepseek"


async def test_missing_model_returns_400(monkeypatch):
    result = await make_service(lambda r: httpx.Response(200)).chat({"messages": []})
    assert result.status_code == 400
    assert result.provider == "local"


async def test_unknown_model_returns_404(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    result = await make_service(lambda r: httpx.Response(200)).chat({"model": "nope"})
    assert result.status_code == 404
    assert b"model_not_found" in result.body


async def test_stream_ok_with_first_chunk_commit(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")

    async def gen():
        yield b"data: {\"a\":1}\n\n"
        yield b"data: {\"b\":2}\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=gen())

    result = await make_service(handler).chat({"model": "m1", "stream": True})
    assert result.provider == "sjtu"
    assert result.status_code == 200
    chunks = [c async for c in result.stream]
    assert b"\"a\":1" in chunks[0] and b"\"b\":2" in chunks[1]


async def test_stream_break_before_first_chunk_fails_over(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")

    async def broken():
        raise httpx.RemoteProtocolError("closed before first chunk")
        yield b""  # pragma: no cover

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sjtu.test":
            return httpx.Response(200, content=broken())
        async def gen():
            yield b"data: ok\n\n"
        return httpx.Response(200, content=gen())

    result = await make_service(handler).chat({"model": "m1", "stream": True})
    assert result.provider == "deepseek"


async def test_stream_break_after_first_chunk_not_retried(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    calls = []

    async def broken():
        yield b"data: first\n\n"
        raise httpx.RemoteProtocolError("mid-stream failure")

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "sjtu.test":
            return httpx.Response(200, content=broken())
        return httpx.Response(200, json={"ok": True})

    result = await make_service(handler).chat({"model": "m1", "stream": True})
    assert result.provider == "sjtu"  # 首块后已提交，不再换家
    received = []
    with pytest.raises(httpx.HTTPError):
        async for chunk in result.stream:
            received.append(chunk)
    assert received == [b"data: first\n\n"]
    assert calls == ["sjtu.test"]


async def test_quota_body_triggers_long_cooldown(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sjtu.test":
            return httpx.Response(429, json={"error": "weekly quota exhausted"})
        return httpx.Response(200, json={"ok": True})

    cfg = make_cfg()
    breaker = Breaker(cooldown_429=60, cooldown_quota=1800, cooldown_network=15)
    stats = Stats()
    service = GatewayService(cfg, breaker, stats, {},
                             client_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(handler)))
    result = await service.chat({"model": "m1"})
    assert result.provider == "deepseek"
    assert breaker.cooldown_remaining("sjtu") > 60  # 配额冷却生效


async def test_model_map_applied_to_upstream(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={"ok": True})

    cfg = AppConfig(providers=[
        ProviderConfig(name="sjtu", base_url="https://sjtu.test/v1", api_key_env="SJTU_API_KEY",
                       priority=1, model_map={"m1": "upstream-m1"}),
    ])
    await make_service(handler, cfg).chat({"model": "m1"})
    assert seen["model"] == "upstream-m1"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_forward.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

`gateway/app/forward.py`：

```python
"""转发引擎：候选链尝试 + 宽切换 + SSE 透传（首块前可安全重试）。"""
from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx

from .config import AppConfig, ProviderConfig
from .failover import Breaker, ErrorKind, classify_status
from .providers import build_chain
from .ratelimit import TokenBucket
from .stats import Stats


@dataclass
class GatewayResponse:
    status_code: int
    media_type: str
    provider: str
    body: bytes | None = None
    stream: AsyncIterator[bytes] | None = None

    @property
    def snippet(self) -> str:
        return (self.body or b"")[:512].decode("utf-8", "replace")


def _openai_error(message: str, err_type: str, code: str | None = None) -> bytes:
    return json.dumps(
        {"error": {"message": message, "type": err_type, "code": code}}
    ).encode()


def _provider_key(provider: ProviderConfig) -> str:
    return os.environ.get(provider.api_key_env, "")


class GatewayService:
    def __init__(
        self,
        cfg: AppConfig,
        breaker: Breaker,
        stats: Stats,
        buckets: dict[str, TokenBucket],
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self.cfg = cfg
        self.breaker = breaker
        self.stats = stats
        self.buckets = buckets
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(
                    cfg.failover.connect_timeout_seconds,
                    read=cfg.failover.read_timeout_seconds,
                    write=30.0,
                    pool=5.0,
                )
            )
        )

    async def chat(self, request_body: dict) -> GatewayResponse:
        model = request_body.get("model")
        if not isinstance(model, str) or not model:
            return GatewayResponse(
                400, "application/json", "local",
                body=_openai_error("请求体缺少 model 字段", "invalid_request_error"))
        chain = build_chain(self.cfg, model)
        if not chain:
            return GatewayResponse(
                404, "application/json", "local",
                body=_openai_error(f"没有供应商提供模型 {model}",
                                   "invalid_request_error", "model_not_found"))

        last_failure: GatewayResponse | None = None
        async with self._client_factory() as client:
            for provider, upstream_model in chain:
                if self.breaker.is_open(provider.name):
                    continue
                bucket = self.buckets.get(provider.name)
                if bucket is not None:
                    max_wait = (provider.proactive_rate_limit.max_wait_seconds
                                if provider.proactive_rate_limit else 2.0)
                    if not await bucket.acquire(max_wait):
                        self.stats.note_soft_saturation(provider.name)
                        self.stats.note_switched_away(provider.name)
                        continue
                try:
                    resp = await self._attempt(client, provider, upstream_model, request_body)
                except httpx.HTTPError:
                    self.stats.record(provider.name, ErrorKind.NETWORK)
                    self.breaker.record_failure(provider.name, ErrorKind.NETWORK)
                    self.stats.note_switched_away(provider.name)
                    continue
                kind = classify_status(resp.status_code, resp.snippet)
                self.stats.record(provider.name, kind)
                if kind is ErrorKind.OK:
                    return resp
                if kind is ErrorKind.CLIENT:
                    return resp  # 请求问题：透传，不切换
                self.breaker.record_failure(provider.name, kind)
                self.stats.note_switched_away(provider.name)
                last_failure = resp

        if last_failure is not None:
            return last_failure
        return GatewayResponse(
            502, "application/json", "local",
            body=_openai_error("所有候选供应商当前不可用（熔断或网络故障）",
                               "gateway_error", "all_providers_failed"))

    async def _attempt(
        self,
        client: httpx.AsyncClient,
        provider: ProviderConfig,
        upstream_model: str,
        request_body: dict,
    ) -> GatewayResponse:
        url = provider.base_url + "/chat/completions"
        payload = dict(request_body)
        payload["model"] = upstream_model
        headers = {
            "Authorization": f"Bearer {_provider_key(provider)}",
            "Content-Type": "application/json",
        }
        request = client.build_request("POST", url, json=payload, headers=headers)

        if not payload.get("stream"):
            response = await client.send(request)
            body = await response.aread()
            return GatewayResponse(
                response.status_code,
                response.headers.get("content-type", "application/json"),
                provider.name,
                body=body,
            )

        # 流式：先取到首块才提交；首块前失败可安全换家
        response = await client.send(request, stream=True)
        if response.status_code != 200:
            body = await response.aread()
            await response.aclose()
            return GatewayResponse(
                response.status_code,
                response.headers.get("content-type", "application/json"),
                provider.name,
                body=body,
            )
        chunks = response.aiter_bytes()
        try:
            first = await anext(chunks)
        except StopAsyncIteration:
            await response.aclose()
            raise httpx.RemoteProtocolError("upstream closed before first chunk") from None
        except httpx.HTTPError:
            await response.aclose()
            raise

        async def stream() -> AsyncIterator[bytes]:
            try:
                yield first
                async for chunk in chunks:
                    yield chunk
            finally:
                await response.aclose()

        return GatewayResponse(
            200, response.headers.get("content-type", "text/event-stream"),
            provider.name, stream=stream(),
        )

    async def list_models(self) -> GatewayResponse:
        async with self._client_factory() as client:
            for provider in self.cfg.providers:
                if not provider.available or self.breaker.is_open(provider.name):
                    continue
                try:
                    response = await client.get(
                        provider.base_url + "/models",
                        headers={"Authorization": f"Bearer {_provider_key(provider)}"},
                    )
                except httpx.HTTPError:
                    continue
                if response.status_code == 200:
                    return GatewayResponse(
                        200,
                        response.headers.get("content-type", "application/json"),
                        provider.name,
                        body=response.content,
                    )
        ids = sorted({
            m
            for p in self.cfg.providers if p.available
            for m in [*p.models, *p.model_map]
        })
        body = json.dumps({
            "object": "list",
            "data": [{"id": m, "object": "model", "owned_by": "gateway"} for m in ids],
        }).encode()
        return GatewayResponse(200, "application/json", "config", body=body)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_forward.py -v`
Expected: PASS（全部用例，注意 `test_stream_break_after_first_chunk_not_retried` 验证不重复输出）

- [ ] **Step 5: 提交**

```bash
git add gateway/app/forward.py gateway/tests/test_forward.py
git commit -m "feat: 转发引擎（宽切换/熔断/SSE 首块提交）"
```

---

### Task 9: HTTP 端点组装（app/main.py 扩展）

**Files:**
- Modify: `gateway/app/main.py`（整体替换）
- Test: `gateway/tests/test_main.py`

**Interfaces:**
- Consumes: `load_config`、`Breaker`、`Stats`、`TokenBucket`、`GatewayService/GatewayResponse`
- Produces: `create_app(cfg: AppConfig | None = None, service: GatewayService | None = None) -> FastAPI`
  - `POST /v1/chat/completions`：透传结果，响应头 `X-Gateway-Provider`；流式返回 `StreamingResponse`
  - `GET /v1/models`、`GET /health`（status + 每供应商 available/breaker 剩余冷却）、`GET /stats`
  - 鉴权中间件：环境变量 `GATEWAY_API_KEY` 非空时校验 `Authorization: Bearer <key>`，失败本地 401（OpenAI 错误格式）
  - 模块级 `app = create_app()`（启动时读 `GATEWAY_CONFIG` 环境变量指向的配置，默认 `config.yaml`）

- [ ] **Step 1: 写失败测试**

`gateway/tests/test_main.py`：

```python
import os

import httpx
from fastapi.testclient import TestClient

from app.config import load_config
from app.failover import Breaker
from app.forward import GatewayService
from app.main import create_app
from app.stats import Stats

PUBLIC = ["93.184.216.34"]
RES = lambda host: PUBLIC

CONFIG_YAML = """
providers:
  - name: sjtu
    base_url: https://sjtu.test/v1
    api_key_env: SJTU_API_KEY
    models: [m1]
    priority: 1
"""

MOCK_HANDLER = lambda request: httpx.Response(
    200, json={"id": "x", "choices": [{"message": {"content": "hi"}}]})


def make_client(monkeypatch, gateway_key=None, handler=MOCK_HANDLER):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    cfg = load_config_from_string()
    service = GatewayService(
        cfg, Breaker(), Stats(), {},
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    if gateway_key is not None:
        monkeypatch.setenv("GATEWAY_API_KEY", gateway_key)
    else:
        monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    return TestClient(create_app(cfg=cfg, service=service))


def load_config_from_string(tmp=None):
    import pathlib, tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(CONFIG_YAML)
        path = fh.name
    return load_config(path, environ={"SJTU_API_KEY": "s"}, resolver=RES)


def test_chat_completions_happy_path(monkeypatch):
    client = make_client(monkeypatch)
    resp = client.post("/v1/chat/completions", json={"model": "m1", "messages": []})
    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "sjtu"
    assert resp.json()["id"] == "x"


def test_auth_required_when_key_set(monkeypatch):
    client = make_client(monkeypatch, gateway_key="secret")
    assert client.post("/v1/chat/completions", json={"model": "m1"}).status_code == 401
    ok = client.post("/v1/chat/completions", json={"model": "m1"},
                     headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200


def test_unknown_model_404(monkeypatch):
    client = make_client(monkeypatch)
    resp = client.post("/v1/chat/completions", json={"model": "nope"})
    assert resp.status_code == 404


def test_models_fallback_to_static(monkeypatch):
    def failing(request):
        raise httpx.ConnectError("down")
    client = make_client(monkeypatch, handler=failing)
    resp = client.get("/v1/models")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "m1"


def test_stats_endpoint(monkeypatch):
    client = make_client(monkeypatch)
    client.post("/v1/chat/completions", json={"model": "m1"})
    snap = client.get("/stats").json()
    assert snap["sjtu"]["success"] == 1


def test_health_endpoint(monkeypatch):
    client = make_client(monkeypatch)
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["providers"]["sjtu"]["available"] is True
```

- [ ] **Step 2: 运行确认失败**

Run: `cd gateway && uv run pytest tests/test_main.py -v`
Expected: FAIL（create_app 不接受参数 / 端点不存在）

- [ ] **Step 3: 实现（整体替换 app/main.py）**

```python
"""FastAPI 入口：本地 OpenAI 兼容端点。"""
from __future__ import annotations

import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AppConfig, load_config
from .failover import Breaker
from .forward import GatewayResponse, GatewayService
from .ratelimit import TokenBucket
from .stats import Stats


def _build_service(cfg: AppConfig) -> GatewayService:
    breaker = Breaker(
        cfg.failover.cooldown_429_seconds,
        cfg.failover.cooldown_quota_seconds,
        cfg.failover.cooldown_network_seconds,
    )
    buckets: dict[str, TokenBucket] = {}
    for p in cfg.providers:
        if p.proactive_rate_limit:
            buckets[p.name] = TokenBucket(
                p.proactive_rate_limit.requests_per_minute,
                p.proactive_rate_limit.burst,
            )
    return GatewayService(cfg, breaker, Stats(), buckets)


def create_app(cfg: AppConfig | None = None, service: GatewayService | None = None) -> FastAPI:
    cfg = cfg or load_config(os.environ.get("GATEWAY_CONFIG", "config.yaml"))
    service = service or _build_service(cfg)
    app = FastAPI(title="sjtu-llm-gateway")

    @app.middleware("http")
    async def require_gateway_key(request: Request, call_next):
        expected = os.environ.get("GATEWAY_API_KEY")
        if expected:
            supplied = request.headers.get("authorization", "")
            if supplied != f"Bearer {expected}":
                return JSONResponse(status_code=401, content={
                    "error": {"message": "无效的网关密钥",
                              "type": "invalid_request_error"}})
        return await call_next(request)

    def _to_response(result: GatewayResponse) -> Response:
        headers = {"X-Gateway-Provider": result.provider}
        if result.stream is not None:
            return StreamingResponse(result.stream, status_code=result.status_code,
                                     media_type=result.media_type, headers=headers)
        return Response(content=result.body, status_code=result.status_code,
                        media_type=result.media_type, headers=headers)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        body = await request.json()
        return _to_response(await service.chat(body))

    @app.get("/v1/models")
    async def models() -> Response:
        return _to_response(await service.list_models())

    @app.get("/health")
    async def health() -> dict:
        providers = {}
        for p in cfg.providers:
            providers[p.name] = {
                "available": p.available,
                "breaker_open": service.breaker.is_open(p.name),
                "cooldown_remaining": round(service.breaker.cooldown_remaining(p.name), 1),
                "unavailable_reason": p.unavailable_reason or None,
            }
        return {"status": "ok", "providers": providers}

    @app.get("/stats")
    async def stats() -> dict:
        return service.stats.snapshot()

    return app


app = create_app()
```

- [ ] **Step 4: 运行全量测试确认通过**

Run: `cd gateway && uv run pytest -v`
Expected: 全部 PASS（含 Task 1 的旧 /health 断言——注意旧断言 `resp.json() == {"status": "ok"}` 需要更新为包含 providers 字段）

`gateway/tests/test_health.py` 更新为：

```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_health_returns_ok():
    client = TestClient(create_app())
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
```

（`create_app()` 无配置时读取默认 `config.yaml`，在 `gateway/` 下放一份测试用 `config.yaml` 或将旧断言留在 Task 10 的注入式测试中——采用后者：本测试文件改为注入 cfg，见 Step 5。）

- [ ] **Step 5: 修正 test_health.py 用注入配置**

```python
from fastapi.testclient import TestClient

from app.config import load_config
from app.main import create_app


def test_health_returns_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n  - name: sjtu\n    base_url: https://sjtu.test/v1\n"
        "    api_key_env: SJTU_API_KEY\n    models: [m1]\n    priority: 1\n",
        encoding="utf-8")
    cfg = load_config(str(path), resolver=lambda host: ["93.184.216.34"])
    resp = TestClient(create_app(cfg=cfg)).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
```

Run: `cd gateway && uv run pytest -v`
Expected: 全部 PASS

- [ ] **Step 6: 提交**

```bash
git add gateway/app/main.py gateway/tests/test_main.py gateway/tests/test_health.py
git commit -m "feat: HTTP 端点组装（鉴权/模型列表/健康/统计）"
```

---

### Task 10: HTTP 层集成测试（多供应商场景矩阵）

**Files:**
- Test: `gateway/tests/test_integration.py`

**Interfaces:**
- Consumes: Task 9 的 `create_app(cfg, service)` 与 mock 注入方式；验证真实 HTTP 语义（响应头、状态码、流式响应）。

- [ ] **Step 1: 写集成测试**

`gateway/tests/test_integration.py`：

```python
import httpx
from fastapi.testclient import TestClient

from app.config import load_config
from app.failover import Breaker
from app.forward import GatewayService
from app.main import create_app
from app.ratelimit import TokenBucket
from app.stats import Stats

PUBLIC = lambda host: ["93.184.216.34"]


def build(tmp_path, handler, monkeypatch, env=None):
    for k, v in (env or {"SJTU_API_KEY": "s", "DEEPSEEK_API_KEY": "d"}).items():
        monkeypatch.setenv(k, v)
    path = tmp_path / "config.yaml"
    path.write_text(
        "providers:\n"
        "  - name: sjtu\n    base_url: https://sjtu.test/v1\n"
        "    api_key_env: SJTU_API_KEY\n    models: [m1]\n    priority: 1\n"
        "  - name: deepseek\n    base_url: https://deepseek.test/v1\n"
        "    api_key_env: DEEPSEEK_API_KEY\n    models: [m1]\n    priority: 2\n",
        encoding="utf-8")
    cfg = load_config(str(path), resolver=PUBLIC)
    service = GatewayService(cfg, Breaker(), Stats(), {},
                             client_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(handler)))
    return TestClient(create_app(cfg=cfg, service=service))


def test_rate_limit_scenario_switches_provider_and_records_stats(tmp_path, monkeypatch):
    state = {"sjtu_calls": 0}

    def handler(request):
        if request.url.host == "sjtu.test":
            state["sjtu_calls"] += 1
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"id": "y", "choices": []})

    client = build(tmp_path, handler, monkeypatch)
    for _ in range(3):
        resp = client.post("/v1/chat/completions", json={"model": "m1"})
        assert resp.status_code == 200
        assert resp.headers["x-gateway-provider"] == "deepseek"
    stats = client.get("/stats").json()
    assert stats["sjtu"]["rate_limited"] == 1          # 第二次起熔断直接跳过
    assert stats["deepseek"]["success"] == 3
    assert state["sjtu_calls"] == 1


def test_streaming_response_passthrough(tmp_path, monkeypatch):
    async def gen():
        yield b"data: {\"delta\": \"你\"}\n\n"
        yield b"data: {\"delta\": \"好\"}\n\n"
        yield b"data: [DONE]\n\n"

    client = build(tmp_path, lambda r: httpx.Response(
        200, headers={"content-type": "text/event-stream"}, content=gen()), monkeypatch)
    with client.stream("POST", "/v1/chat/completions",
                       json={"model": "m1", "stream": True}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = b"".join(resp.iter_bytes())
    assert b"[DONE]" in body


def test_no_auth_needed_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    client = build(tmp_path, lambda r: httpx.Response(200, json={"id": "z"}), monkeypatch)
    assert client.post("/v1/chat/completions", json={"model": "m1"}).status_code == 200


def test_provider_with_missing_key_is_skipped(tmp_path, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, json={"id": "z"})

    client = build(tmp_path, handler, monkeypatch,
                   env={"SJTU_API_KEY": "", "DEEPSEEK_API_KEY": "d"})
    resp = client.post("/v1/chat/completions", json={"model": "m1"})
    assert resp.headers["x-gateway-provider"] == "deepseek"
    assert calls == ["deepseek.test"]
```

- [ ] **Step 2: 运行确认通过**

Run: `cd gateway && uv run pytest tests/test_integration.py -v`
Expected: PASS（若 FAIL 按错误修 main/forward，此任务暴露的是组装层问题）

- [ ] **Step 3: 全量回归**

Run: `cd gateway && uv run pytest -v`
Expected: 全部 PASS

- [ ] **Step 4: 提交**

```bash
git add gateway/tests/test_integration.py
git commit -m "test: HTTP 层多供应商切换集成测试"
```

---

### Task 11: VPN 容器（strongSwan）

**Files:**
- Create: `vpn/Dockerfile`
- Create: `vpn/swanctl/swanctl.conf.template`
- Create: `vpn/entrypoint.sh`
- Create: `vpn/.gitignore`（防止误提交渲染后的真实配置：`swanctl.conf`）

**Interfaces:**
- Consumes: 环境变量 `VPN_SERVER`（默认 `stu.vpn.sjtu.edu.cn`）、`VPN_USERNAME`、`VPN_PASSWORD`、`VPN_ROUTE_SUBNETS`（默认 `202.120.0.0/16`）、`VPN_EXTRA_HOSTS`（默认 `models.sjtu.edu.cn`，空格分隔）
- Produces: 容器内 strongSwan 以 CHILD `sjtu` 建立仅覆盖 `remote_ts` 的 IKEv2 隧道 + 30 秒周期探测自愈；探测命令 `curl -m 8 -s -o /dev/null -w '%{http_code}' https://models.sjtu.edu.cn/api/v1/models`（非 `000` 即网络可达）。

- [ ] **Step 1: Dockerfile**

`vpn/Dockerfile`：

```dockerfile
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        strongswan libstrongswan-extra-plugins curl iproute2 dnsutils gettext-base \
    && rm -rf /var/lib/apt/lists/*

COPY swanctl/ /etc/swanctl/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
```

- [ ] **Step 2: swanctl 模板**

`vpn/swanctl/swanctl.conf.template`（占位符由 envsubst 渲染）：

```ini
connections {
    sjtu {
        local_addrs  = %defaultroute
        remote_addrs = ${VPN_SERVER}
        version = 2
        # 学校网关提案未知，spike 时按需收窄/调整
        proposals = aes256-sha256-modp2048,aes128-sha256-modp2048,aes256-sha1-modp1024,default
        send_cert = never
        fragmentation = yes
        mobike = yes
        local {
            auth = eap-mschapv2
            eap_id = ${VPN_USERNAME}
        }
        remote {
            auth = pubkey
        }
        children {
            sjtu {
                # 关键：流量选择器只圈定交大网段，不做默认路由接管
                remote_ts = ${VPN_TS}
                start_action = none
                dpd_action = restart
                close_action = restart
            }
        }
    }
}
secrets {
    eap-sjtu {
        id = ${VPN_USERNAME}
        secret = ${VPN_PASSWORD}
    }
}
```

- [ ] **Step 3: entrypoint.sh**

```sh
#!/bin/sh
# 应用内 VPN：建立仅覆盖交大网段的 IKEv2 隧道并保活。
set -eu

: "${VPN_SERVER:=stu.vpn.sjtu.edu.cn}"
: "${VPN_USERNAME:?需要 jAccount 用户名（VPN_USERNAME）}"
: "${VPN_PASSWORD:?需要 jAccount 密码（VPN_PASSWORD）}"
: "${VPN_ROUTE_SUBNETS:=202.120.0.0/16}"
: "${VPN_EXTRA_HOSTS:=models.sjtu.edu.cn}"
PROBE_URL="${PROBE_URL:-https://models.sjtu.edu.cn/api/v1/models}"

# 1. 额外主机解析为 IP，并入流量选择器
TS="$VPN_ROUTE_SUBNETS"
for host in $VPN_EXTRA_HOSTS; do
    ip="$(dig +short "$host" A | tail -1 || true)"
    if [ -n "$ip" ]; then
        TS="$TS,$ip/32"
        echo "[vpn] $host -> $ip/32 纳入隧道"
    else
        echo "[vpn] 警告: 解析 $host 失败，跳过"
    fi
done
export VPN_TS="$TS"
echo "[vpn] 流量选择器: $TS"

# 2. 渲染 swanctl 配置（密码不落日志）
envsubst '${VPN_SERVER} ${VPN_USERNAME} ${VPN_PASSWORD} ${VPN_TS}' \
    < /etc/swanctl/swanctl.conf.template > /etc/swanctl/swanctl.conf
chmod 600 /etc/swanctl/swanctl.conf

# 3. 不让 charon 安装任何路由（隧道范围完全由 remote_ts 决定）
printf 'install_routes = no\n' > /etc/strongswan.d/charon/no-routes.conf

# 4. 启动 charon 并加载配置
mkdir -p /var/run/charon
/usr/lib/ipsec/strongswan start --nofork > /var/log/charon.log 2>&1 &

for i in $(seq 1 30); do
    swanctl --load-all >/dev/null 2>&1 && break
    sleep 1
done
swanctl --load-all
echo "[vpn] 发起连接..."
swanctl --initiate --child sjtu || echo "[vpn] 首次发起失败，进入保活重试"

# 5. 保活：周期探测，失败重连
while true; do
    code="$(curl -m 8 -s -o /dev/null -w '%{http_code}' "$PROBE_URL" || echo 000)"
    if [ "$code" = "000" ]; then
        echo "$(date '+%F %T') [vpn] 探测失败($code)，重连隧道"
        swanctl --terminate --child sjtu >/dev/null 2>&1 || true
        sleep 2
        swanctl --initiate --child sjtu >/dev/null 2>&1 || true
    else
        echo "$(date '+%F %T') [vpn] 探测 OK (HTTP $code)"
    fi
    sleep 30
done
```

`vpn/.gitignore`：

```
swanctl.conf
```

- [ ] **Step 4: 本地构建验证**

Run: `docker build -t sjtu-vpn ./vpn`
Expected: 构建成功（需外网拉基础镜像与 apt 包）

Run: `docker run --rm -e VPN_USERNAME=demo -e VPN_PASSWORD=demo sjtu-vpn sh -c 'envsubst < /etc/swanctl/swanctl.conf.template | head -20'`
Expected: 模板占位符被正确替换（VPN_TS 含 202.120.0.0/16）

- [ ] **Step 5: 提交**

```bash
git add vpn/
git commit -m "build: strongSwan IKEv2 VPN 容器（仅交大网段流量选择器）"
```

---

### Task 12: 网关镜像与 Compose 编排

**Files:**
- Create: `gateway/Dockerfile`
- Create: `gateway/.dockerignore`
- Create: `docker-compose.yml`
- Create: `.env.example`（仓库根）

**Interfaces:**
- Consumes: Task 11 的 vpn 镜像；Task 1-10 的网关应用
- Produces: `docker compose up -d` 拉起全栈；宿主机 `127.0.0.1:8000` 可访问网关。

- [ ] **Step 1: 网关镜像**

`gateway/Dockerfile`：

```dockerfile
FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app

CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`gateway/.dockerignore`：

```
.venv/
__pycache__/
tests/
```

- [ ] **Step 2: docker-compose.yml**

```yaml
services:
  vpn:
    build: ./vpn
    cap_add: [NET_ADMIN]
    devices:
      - /dev/net/tun:/dev/net/tun
    env_file: .env
    environment:
      VPN_SERVER: stu.vpn.sjtu.edu.cn
      VPN_ROUTE_SUBNETS: 202.120.0.0/16
      VPN_EXTRA_HOSTS: models.sjtu.edu.cn
    ports:
      - "127.0.0.1:8000:8000"   # 端口由 vpn 服务发布（网关共享其 netns）
    restart: unless-stopped

  gateway:
    build: ./gateway
    network_mode: "service:vpn"
    env_file: .env
    environment:
      GATEWAY_CONFIG: /app/config.yaml
    volumes:
      - ./config.yaml:/app/config.yaml:ro
    restart: unless-stopped
    depends_on:
      - vpn
```

- [ ] **Step 3: .env.example**

```
# 复制为 .env 并填写；.env 已被 gitignore，严禁提交
SJTU_API_KEY=
DEEPSEEK_API_KEY=
GATEWAY_API_KEY=
VPN_USERNAME=
VPN_PASSWORD=
```

- [ ] **Step 4: 构建验证**

Run: `cp .env.example .env && cp config.example.yaml config.yaml && docker compose build`
Expected: 两个镜像构建成功（之后 `rm .env` 中的空值不影响 build）

Run: `docker compose config` 校验编排合法性（gateway 会因缺密钥标记供应商不可用——属预期）

- [ ] **Step 5: 提交**

```bash
git add gateway/Dockerfile gateway/.dockerignore docker-compose.yml .env.example
git commit -m "build: 网关镜像与 compose 编排（共享 netns）"
```

---

### Task 13: Spike——真实 IKEv2 连通性验证（需用户提供凭据）

**Files:**
- Create: `docs/superpowers/notes/2026-08-31-vpn-spike.md`（记录结论）

**Interfaces:**
- Consumes: Task 11 vpn 容器、`.env` 中真实 `VPN_USERNAME/VPN_PASSWORD`
- Produces: 连通性结论（成功：记录生效的 proposals/auth；失败：记录尝试过的组合与错误，触发降级决策）

> **用户依赖**：此任务执行前请用户把 jAccount 用户名/密码填入 `.env`（以及可选的 SJTU_API_KEY）。若暂时无法提供，跳过本任务先做 Task 14 的校园网直连验证，凭据就绪后再回来。

- [ ] **Step 1: 启动并观察协商日志**

Run: `docker compose up vpn`（前台观察）
Expected 日志：`CHILD_UP` / `established` 字样；若 `AUTHENTICATION_FAILED` → Step 2；若 `NO_PROPOSAL_CHOSEN` / `INVALID_KE_PAYLOAD` → Step 3

- [ ] **Step 2: 认证失败时排查顺序**

依次尝试（每次改 `swanctl.conf.template` 后 `docker compose up --build vpn`）：
1. `local.auth = eap-md5`（+secrets 对应改）
2. `eap_id` 与 secrets `id` 用完整 jaccount（如 `liuhaohui`，不带 @sjtu.edu.cn 与带域名的两种都试）
3. `remote.auth = pubkey` 改为 `psk` 临时验证是否证书校验问题（仅诊断用，不复用）

- [ ] **Step 3: 提案不匹配时排查顺序**

`proposals` 依次换为：
1. `aes256-sha1-modp1024,3des-sha1-modp1024`
2. `aes128-sha256-modp2048,aes256-sha384-modp2048`
3. `default`（让 charon 用内置全集）

- [ ] **Step 4: 成功判据（最终审查修订版）**

Run: `docker compose exec vpn curl -m 8 -s -o /dev/null -w '%{http_code}' https://models.sjtu.edu.cn/api/v1/models`
Expected: `401` 或 `200`（而非 `000`）——注意本机当前可直连，即使隧道未建立探测也会返回 401，**必须以下列四项为准**：
1. `docker compose exec vpn ip xfrm policy` —— 选择器确为交大 TS（202.120.0.0/16 + 模型 API IP/32），而非 0.0.0.0/0；
2. `docker compose exec vpn ip xfrm state` 有 esp 状态且其 bytes 计数随请求**增长**（证明流量真的进隧道，非仅 SA 建立）；
3. 记录学校网关是否分配 VIP（`ip addr` 新增地址 / charon 日志 `virtual IP`）；
4. 若出现包绕过隧道（`install_routes = no` 下源地址选择不匹配 xfrm 模板的明文绕过陷阱）：当场决策翻转 `no-routes.conf` 为 `install_routes = yes`——remote_ts 已显式协商、IKEv2 narrowing 只缩不放，不会接管默认路由；翻转后重复 1-3 验证并记入 spike 笔记。

- [ ] **Step 5: 断网自愈验证（可选，校外环境下做最有意义）**

`docker compose stop vpn && docker compose start vpn`，观察 30 秒内重新 CHILD_UP。

- [ ] **Step 6: 记录结论并提交**

`docs/superpowers/notes/2026-08-31-vpn-spike.md`：生效配置、尝试记录、是否触发降级（降级 = 改用 docker-easyconnect，属设计变更，需回到用户确认后再实现）。

```bash
git add docs/superpowers/notes/2026-08-31-vpn-spike.md vpn/swanctl/swanctl.conf.template
git commit -m "docs: VPN spike 结论（生效提案/认证方式）"
```

---

### Task 14: 端到端验收 + AGENTS.md 更新

**Files:**
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: 全部前序任务；`.env` 中 `SJTU_API_KEY`（用户提供）。

- [ ] **Step 1: 全栈启动**

Run: `docker compose up -d && docker compose ps`
Expected: 两容器 Up（vpn 健康，gateway 无崩溃重启）

- [ ] **Step 2: 真实非流式调用**

```bash
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"说\"连通\"两个字"}]}'
```
Expected: 200，`X-Gateway-Provider: sjtu`，回复含"连通"

- [ ] **Step 3: 真实流式调用**

```bash
curl -sN http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-chat","stream":true,"messages":[{"role":"user","content":"数到3"}]}'
```
Expected: SSE 分块输出，结尾 `data: [DONE]`

- [ ] **Step 4: 限速切换验证**

```bash
for i in $(seq 1 15); do
  curl -s -o /dev/null -w "%{http_code} %{header_json}\n" \
    http://127.0.0.1:8000/v1/chat/completions \
    -H 'Content-Type: application/json' \
    -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}' \
    | grep -o 'x-gateway-provider[^,]*'
done
```
Expected: 前几次为 `sjtu`，触发 429/软饱和后出现 `deepseek`；`curl -s http://127.0.0.1:8000/stats` 可见 `rate_limited`/`switched_away`/`soft_saturated` 计数。冷却 60 秒后再试恢复 `sjtu`。

- [ ] **Step 5: 日志脱敏抽查**

Run: `docker compose logs gateway | grep -iE 'authorization|sk-|bearer' || echo clean`
Expected: `clean`（无密钥泄漏）

- [ ] **Step 6: 更新 AGENTS.md**

在 `AGENTS.md` 的 "Build / test" 节替换为实际命令：

```markdown
## Build / test / run

- 网关单测/全测：`cd gateway && uv run pytest`
- 全栈启动：`cp .env.example .env && cp config.example.yaml config.yaml`（填密钥）→ `docker compose up -d`
- 排障：`docker compose logs -f vpn gateway`；`curl http://127.0.0.1:8000/health`
- VPN 容器 spike 记录：docs/superpowers/notes/2026-08-31-vpn-spike.md
- 设计文档：docs/superpowers/specs/2026-08-31-sjtu-llm-gateway-design.md
```

- [ ] **Step 7: 提交**

```bash
git add AGENTS.md
git commit -m "docs: AGENTS.md 补充构建/运行命令"
```

---

## Self-Review 记录

- 规格覆盖：FR1（Task 8/9/10）、FR2/FR3（Task 11/13）、FR4（Task 3/4）、FR5（Task 6/8）、FR6（Task 5/8）、FR7（Task 9）、FR8（Task 9）——全覆盖；验收标准 1-6 分别对应 Task 14 Step 2-5、Task 3/12、Task 13。
- 类型一致性：`GatewayResponse(body/stream)`、`Breaker(cooldown_429, cooldown_quota, cooldown_network)`、`load_config(path, environ, resolver)`、`create_app(cfg, service)` 在各任务间已核对一致。
- 占位符：Task 13 的排查步骤是"决策树"而非占位符（spike 本质是探索，每步都有明确的下一步）。
