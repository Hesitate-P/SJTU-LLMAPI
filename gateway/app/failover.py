"""上游错误分类与按供应商熔断。冷却到期真半开（单探测准入），探测成功恢复全量、失败再冷却。"""
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
        self._probing: set[str] = set()

    def _now(self, now: float | None) -> float:
        return time.monotonic() if now is None else now

    def is_open(self, provider: str, now: float | None = None) -> bool:
        # 冷却未到期，或半开探测进行中（对外一致显示为开）
        if provider in self._probing:
            return True
        return self._until.get(provider, 0.0) > self._now(now)

    def acquire_probe(self, provider: str, now: float | None = None) -> bool:
        """冷却到期后的准入闸（真半开）：恰允许一个请求作为探测。

        - 冷却未到期或他者探测中 → False（跳过该供应商）
        - 冷却刚到期（打开过，``_until > 0``）→ 置 probing 并返回 True，此请求即探测
        - 从未打开过（``_until == 0``）→ True，不设标志（全放行）
        """
        if self.is_open(provider, now):
            return False
        if self._until.get(provider, 0.0) > 0:
            self._probing.add(provider)
        return True

    def release_probe(self, provider: str) -> None:
        """探测者放弃（如令牌桶超时软饱和）：清标志，允许下一请求再探。"""
        self._probing.discard(provider)

    def record_success(self, provider: str) -> None:
        """供应商活着：清冷却与探测标志，恢复全放行。"""
        self._until[provider] = 0.0
        self._probing.discard(provider)

    def cooldown_remaining(self, provider: str, now: float | None = None) -> float:
        return max(0.0, self._until.get(provider, 0.0) - self._now(now))

    def record_failure(self, provider: str, kind: ErrorKind, now: float | None = None) -> float:
        cooldown = self._cooldowns.get(kind, 0.0)
        until = self._now(now) + cooldown
        if until > self._until.get(provider, 0.0):
            self._until[provider] = until
        self._probing.discard(provider)  # 探测失败：清标志，冷却期间无人再入
        return max(until, self._until.get(provider, 0.0))
