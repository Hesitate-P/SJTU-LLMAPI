import json

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
