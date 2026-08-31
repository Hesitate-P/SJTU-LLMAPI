"""对上游的主动限速：连续补充令牌桶，排队超时即放弃（上层转走备用）。"""
from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_minute: float, burst: float = 3.0) -> None:
        if rate_per_minute <= 0:
            raise ValueError(f"rate_per_minute 必须为正数，收到 {rate_per_minute!r}")
        if burst <= 0:
            raise ValueError(f"burst 必须为正数，收到 {burst!r}")
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
