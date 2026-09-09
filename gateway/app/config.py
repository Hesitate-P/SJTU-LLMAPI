"""config.yaml 加载与校验。密钥只从环境变量读，绝不落盘。"""
from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field

import yaml

from .security import assert_safe_upstream_url

logger = logging.getLogger("gateway")


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
    context_window: int | None = None
    model_contexts: dict[str, int] = field(default_factory=dict)
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


# FR10 内置上下文窗口表：按模型名维度，任何供应商的同名模型同样适用。
# 不内置按供应商名的假设（如 DeepSeek 官方渠道 131072），由显式配置声明，避免误配。
DEFAULT_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-chat": 262144,
    "deepseek-reasoner": 262144,
    "qwen": 262144,
    "qwen3.6-27b": 262144,
    "minimax": 196608,
    "minimax-m2.7": 196608,
}

FALLBACK_CONTEXT_WINDOW = 8192


def context_for(provider: ProviderConfig, model: str) -> int:
    """解析 provider 对某（客户端可见）模型的上下文窗口。

    优先级：model_contexts[model] → provider.context_window
    → DEFAULT_CONTEXT_WINDOWS["provider:model"] → DEFAULT_CONTEXT_WINDOWS[model] → 8192。
    """
    if model in provider.model_contexts:
        return provider.model_contexts[model]
    if provider.context_window is not None:
        return provider.context_window
    qualified = f"{provider.name}:{model}"
    if qualified in DEFAULT_CONTEXT_WINDOWS:  # 为将来 provider 级覆盖留口，当前内置表无此类键
        return DEFAULT_CONTEXT_WINDOWS[qualified]
    if model in DEFAULT_CONTEXT_WINDOWS:
        return DEFAULT_CONTEXT_WINDOWS[model]
    return FALLBACK_CONTEXT_WINDOW


def _resolve_config_path(path: str) -> str:
    """配置路径白名单：只允许工作目录、/app 或系统临时目录内的文件（纵深防御）。"""
    resolved = os.path.realpath(path)
    roots = [os.path.realpath(os.getcwd()), "/app", tempfile.gettempdir()]
    if not any(resolved == root or resolved.startswith(root + os.sep) for root in roots):
        raise ValueError(
            f"配置路径越界：{path!r}（解析为 {resolved}），只允许工作目录、/app 或系统临时目录内"
        )
    return resolved


def load_config(
    path: str,
    environ: Mapping[str, str] | None = None,
    resolver=None,
) -> AppConfig:
    environ = os.environ if environ is None else environ
    with open(_resolve_config_path(path), encoding="utf-8") as fh:
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
        raw_context_window = item.get("context_window")
        provider = ProviderConfig(
            name=str(item["name"]),
            base_url=str(item["base_url"]).rstrip("/"),
            api_key_env=str(item["api_key_env"]),
            priority=int(item["priority"]),
            models=[str(m) for m in item.get("models", [])],
            model_map={str(k): str(v) for k, v in item.get("model_map", {}).items()},
            context_window=int(raw_context_window) if raw_context_window is not None else None,
            model_contexts={str(k): int(v) for k, v in item.get("model_contexts", {}).items()},
        )
        rl = item.get("proactive_rate_limit")
        if rl:
            requests_per_minute = int(rl["requests_per_minute"])
            burst = int(rl.get("burst", 3))
            if requests_per_minute <= 0:
                raise ValueError(
                    f"{path} 中 provider {provider.name} 的 proactive_rate_limit.requests_per_minute "
                    f"必须为正数，收到 {requests_per_minute}"
                )
            if burst <= 0:
                raise ValueError(
                    f"{path} 中 provider {provider.name} 的 proactive_rate_limit.burst "
                    f"必须为正数，收到 {burst}"
                )
            provider.proactive_rate_limit = RateLimitConfig(
                requests_per_minute=requests_per_minute,
                max_wait_seconds=float(rl.get("max_wait_seconds", 2.0)),
                burst=burst,
            )
        assert_safe_upstream_url(provider.base_url, resolver=resolver)
        if not environ.get(provider.api_key_env):
            provider.available = False
            provider.unavailable_reason = f"环境变量 {provider.api_key_env} 未设置"
            # 只记环境变量名，绝不记密钥值
            logger.warning("provider %s 不可用：环境变量 %s 未设置",
                           provider.name, provider.api_key_env)
        cfg.providers.append(provider)

    if not cfg.providers:
        raise ValueError("config.yaml 中没有任何 provider")
    cfg.providers.sort(key=lambda p: p.priority)

    # FR10 单调校验：按客户端可见模型分组（models + model_map 键并集），
    # 组内按 priority 升序；窗口严格变小只告警不拒绝（相等或递增都合法）。
    groups: dict[str, list[ProviderConfig]] = {}
    for provider in cfg.providers:
        for model in set(provider.models) | set(provider.model_map):
            groups.setdefault(model, []).append(provider)
    for model, group in groups.items():
        ordered = sorted(group, key=lambda p: p.priority)
        prev = ordered[0]
        for cur in ordered[1:]:
            prev_window, cur_window = context_for(prev, model), context_for(cur, model)
            if cur_window < prev_window:
                logger.warning(
                    "模型 %s 的兜底 %s 窗口 %d 小于 %s 的 %d",
                    model, cur.name, cur_window, prev.name, prev_window,
                )
            prev = cur
    return cfg
