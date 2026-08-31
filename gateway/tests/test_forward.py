import asyncio
import json
import time

import httpx
import pytest

from app.config import AppConfig, ProviderConfig
from app.failover import Breaker, ErrorKind
from app.forward import GatewayService
from app.stats import Stats


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
        resolver=lambda host: ["93.184.216.34"],
    )


def explicit_service(handler, breaker, stats, cfg=None):
    """显式注入 breaker/stats，便于断言熔断与统计副作用。"""
    cfg = cfg or make_cfg()
    return GatewayService(
        cfg, breaker, stats, {},
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)),
        resolver=lambda host: ["93.184.216.34"],
    )


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
                                 transport=httpx.MockTransport(handler)),
                             resolver=lambda host: ["93.184.216.34"])
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

    breaker = Breaker()
    stats = Stats()
    service = explicit_service(handler, breaker, stats)
    result = await service.chat({"model": "m1", "stream": True})
    assert result.provider == "sjtu"  # 首块后已提交，不再换家
    received = []
    with pytest.raises(httpx.HTTPError):
        async for chunk in result.stream:
            received.append(chunk)
    assert received == [b"data: first\n\n"]
    assert calls == ["sjtu.test"]
    assert breaker.is_open("sjtu") is True  # 流中断记熔断
    assert stats.snapshot()["sjtu"]["network_errors"] >= 1  # 记统计（不双计 requests）


async def test_stream_completion_records_success(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")

    async def gen():
        yield b"data: one\n\n"
        yield b"data: two\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=gen())

    breaker = Breaker()
    service = explicit_service(handler, breaker, Stats())
    result = await service.chat({"model": "m1", "stream": True})
    assert result.provider == "sjtu"
    first = await anext(result.stream)  # 首块
    assert first == b"data: one\n\n"
    breaker.record_failure("sjtu", ErrorKind.NETWORK)  # 消费期间进入冷却
    assert breaker.is_open("sjtu") is True
    rest = [chunk async for chunk in result.stream]  # 正常消费完剩余块
    assert rest == [b"data: two\n\n"]
    assert breaker.is_open("sjtu") is False  # 流正常完成 → record_success 恢复


async def test_half_open_single_probe_after_cooldown_expiry(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "sjtu.test":
            return httpx.Response(429, json={"error": "no"})
        return httpx.Response(200, json={"ok": True})

    breaker = Breaker()
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=time.monotonic() - 3600)
    service = explicit_service(handler, breaker, Stats())
    r1 = await service.chat({"model": "m1"})
    assert r1.provider == "deepseek"
    assert calls == ["sjtu.test", "deepseek.test"]  # 首个请求以探测身份触了 sjtu

    calls.clear()
    r2 = await service.chat({"model": "m1"})
    assert r2.provider == "deepseek"
    assert calls == ["deepseek.test"]  # 探测失败已再冷却：不再触 sjtu


async def test_half_open_concurrent_requests_only_one_probes(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(200, json={"ok": True})

    breaker = Breaker()
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=time.monotonic() - 3600)
    service = explicit_service(handler, breaker, Stats())
    r1, r2 = await asyncio.gather(service.chat({"model": "m1"}),
                                  service.chat({"model": "m1"}))
    # 冷却刚到期的并发请求：恰一个探测 sjtu，另一个直接走 deepseek
    assert sorted(r.provider for r in (r1, r2)) == ["deepseek", "sjtu"]
    assert sorted(calls) == ["deepseek.test", "sjtu.test"]


async def test_probe_flag_released_on_unexpected_error(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("unexpected bug")  # 非 OSError/ValueError/httpx：逃出既有 except

    breaker = Breaker()
    breaker.record_failure("sjtu", ErrorKind.RATE_LIMIT, now=time.monotonic() - 3600)
    service = explicit_service(handler, breaker, Stats())
    with pytest.raises(RuntimeError):
        await service.chat({"model": "m1"})
    # 探测者异常退出后 probing 标志必须已释放：供应商未被永久卡死
    assert breaker.acquire_probe("sjtu") is True


async def test_dns_oserror_fails_over(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    def resolver(host: str) -> list[str]:
        if host == "sjtu.test":
            raise OSError("dns fail")  # 模拟 getaddrinfo 的 gaierror（OSError 子类）
        return ["93.184.216.34"]

    cfg = make_cfg()
    breaker = Breaker()
    stats = Stats()
    service = GatewayService(cfg, breaker, stats, {},
                             client_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(handler)),
                             resolver=resolver)
    result = await service.chat({"model": "m1"})
    assert result.provider == "deepseek"  # DNS 故障切换而非裸 500
    assert stats.snapshot()["sjtu"]["network_errors"] >= 1
    assert breaker.is_open("sjtu") is True  # 记 NETWORK 冷却


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
                                 transport=httpx.MockTransport(handler)),
                             resolver=lambda host: ["93.184.216.34"])
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


async def test_request_time_ssrf_violation_fails_over(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "sjtu.test":
            raise AssertionError("SSRF 违规的供应商不应被请求")
        return httpx.Response(200, json={"ok": True})

    cfg = make_cfg()
    service = GatewayService(cfg, Breaker(), Stats(), {},
                             client_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(handler)),
                             resolver=lambda host: (
                                 ["10.0.0.1"] if host == "sjtu.test"
                                 else ["93.184.216.34"]))  # sjtu.test 解析到内网
    result = await service.chat({"model": "m1"})
    assert result.provider == "deepseek"


async def test_request_time_ssrf_all_violate_returns_502(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    service = GatewayService(make_cfg(), Breaker(), Stats(), {},
                             client_factory=lambda: httpx.AsyncClient(
                                 transport=httpx.MockTransport(
                                     lambda r: httpx.Response(200))),
                             resolver=lambda host: ["127.0.0.1"])
    result = await service.chat({"model": "m1"})
    assert result.status_code == 502
    assert result.provider == "local"


def tracking_service(handler, closes: list, cfg=None):
    """用真实 httpx.AsyncClient 子类统计关闭调用（aclose 与 __aexit__ 都算，
    验证行为而非 mock 内部）。"""

    class TrackingClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            closes.append(1)
            await super().aclose()

        async def __aexit__(self, exc_type, exc_value, traceback) -> None:
            closes.append(1)
            await super().__aexit__(exc_type, exc_value, traceback)

    cfg = cfg or make_cfg()
    return GatewayService(
        cfg, Breaker(), Stats(), {},
        client_factory=lambda: TrackingClient(
            transport=httpx.MockTransport(handler)),
        resolver=lambda host: ["93.184.216.34"],
    )


async def test_stream_client_lives_until_stream_end(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    closes: list[int] = []
    closed_while_consuming = []

    async def gen():
        yield b"data: one\n\n"
        yield b"data: two\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=gen())

    service = tracking_service(handler, closes)
    result = await service.chat({"model": "m1", "stream": True})
    assert result.provider == "sjtu"
    chunks = []
    async for chunk in result.stream:
        chunks.append(chunk)
        closed_while_consuming.append(bool(closes))
    assert chunks == [b"data: one\n\n", b"data: two\n\n"]
    assert not any(closed_while_consuming)  # 消费期间客户端必须存活
    assert closes == [1]  # 流结束后恰好关闭一次


async def test_non_stream_client_closed_after_return(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    closes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    service = tracking_service(handler, closes)
    result = await service.chat({"model": "m1"})
    assert result.status_code == 200
    assert closes == [1]  # chat() 返回后客户端已关闭且仅一次


async def test_all_fail_client_closed_after_502(monkeypatch):
    monkeypatch.setenv("SJTU_API_KEY", "s")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "d")
    closes: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "no"})

    service = tracking_service(handler, closes)
    result = await service.chat({"model": "m1"})
    assert result.status_code == 502
    assert result.provider == "local"
    assert closes == [1]  # 全部失败返回 502 后客户端已关闭且仅一次
