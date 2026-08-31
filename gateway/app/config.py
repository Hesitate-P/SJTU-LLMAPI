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
