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
from .security import assert_safe_upstream_url
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
        resolver: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.cfg = cfg
        self.breaker = breaker
        self.stats = stats
        self.buckets = buckets
        self._resolver = resolver
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
                except (httpx.HTTPError, ValueError):
                    # SSRF 校验失败或网络故障：切换下一家
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
        assert_safe_upstream_url(url, resolver=self._resolver)

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
                    models_url = provider.base_url + "/models"
                    assert_safe_upstream_url(models_url, resolver=self._resolver)
                    response = await client.get(
                        models_url,
                        headers={"Authorization": f"Bearer {_provider_key(provider)}"},
                    )
                except (httpx.HTTPError, ValueError):
                    # SSRF 校验失败或网络故障：切换下一家
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
