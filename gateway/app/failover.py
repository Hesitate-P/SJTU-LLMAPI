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
        return max(until, self._until.get(provider, 0.0))
