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

    def note_midstream_error(self, provider: str) -> None:
        # 只记网络错误，不加 requests：该请求在 chat() 已计过一次，避免双计
        self._bucket(provider)["network_errors"] += 1

    def snapshot(self) -> dict:
        return {name: dict(b) for name, b in self._data.items()}
