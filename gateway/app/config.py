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


def _resolve_config_path(path: str) -> str:
    """配置路径白名单：只允许工作目录、/app 或系统临时目录内的文件。

    GATEWAY_CONFIG 是运维注入的环境变量，仍约束其落点，防止被改成任意路径读取。
    """
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
    return cfg
