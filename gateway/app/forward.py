"""转发引擎：候选链尝试 + 宽切换 + SSE 透传（首块前可安全重试）。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import httpx

from .config import AppConfig, ProviderConfig
from .failover import Breaker, ErrorKind, classify_status
from .providers import build_chain
from .ratelimit import TokenBucket
from .security import assert_safe_upstream_url
from .stats import Stats

logger = logging.getLogger("gateway")

# 切换原因（错误分类 -> 轨迹标签）；CLIENT 透传与成功不产生切换
_SWITCH_REASON = {
    ErrorKind.RATE_LIMIT: "rate_limited",
    ErrorKind.QUOTA: "quota",
    ErrorKind.SERVER: "server",
}


@dataclass
class GatewayResponse:
    status_code: int
    media_type: str
    provider: str
    body: bytes | None = None
    # 流式响应体：消费方必须完整消费或关闭迭代器（Starlette 的
    # StreamingResponse 会做），否则上游连接池泄漏。
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

    def _log_chat(
        self,
        model: object,
        chain_repr: str,
        result: GatewayResponse,
        trace: list[tuple[str, str]],
        start: float,
    ) -> None:
        """单次请求的结构化日志（脱敏：绝不记 Authorization/API key/密钥值）。
        流式响应在返回前记 status=200；流中途中断不补记（保持简单）。"""
        logger.info(
            "model=%s chain=%s provider=%s status=%s elapsed_ms=%d switch_trace=%s",
            model, chain_repr, result.provider, result.status_code,
            int((time.monotonic() - start) * 1000), trace or "-",
        )

    def _note_failure(self, provider: ProviderConfig, trace: list[tuple[str, str]],
                      reason: str) -> None:
        """网络/SSRF 失败的统一记账：统计、熔断、切换计数与轨迹。"""
        self.stats.record(provider.name, ErrorKind.NETWORK)
        self.breaker.record_failure(provider.name, ErrorKind.NETWORK)
        self.stats.note_switched_away(provider.name)
        trace.append((provider.name, reason))

    async def chat(self, request_body: dict) -> GatewayResponse:
        start = time.monotonic()
        trace: list[tuple[str, str]] = []  # 本次请求的切换轨迹：(provider, 原因)
        model = request_body.get("model")
        if not isinstance(model, str) or not model:
            result = GatewayResponse(
                400, "application/json", "local",
                body=_openai_error("请求体缺少 model 字段", "invalid_request_error"))
            self._log_chat(model, "-", result, trace, start)
            return result
        chain = build_chain(self.cfg, model)
        chain_repr = "->".join(p.name for p, _ in chain) or "-"
        if not chain:
            result = GatewayResponse(
                404, "application/json", "local",
                body=_openai_error(f"没有供应商提供模型 {model}",
                                   "invalid_request_error", "model_not_found"))
            self._log_chat(model, chain_repr, result, trace, start)
            return result

        # 手动持有 client：流式响应会把所有权移交给流生成器（由其最终关闭），
        # 其余路径在 finally 统一关闭；循环内的 continue 天然复用同一 client。
        client = self._client_factory()
        handed_over = False
        try:
            for provider, upstream_model in chain:
                if not self.breaker.acquire_probe(provider.name):
                    # 熔断/半开准入失败：本轮未尝试该供应商，无本请求内的原因，不入 trace
                    continue
                # 单个供应商的 attempt 段（bucket 获取 → 分类记账完成）：
                # 任何逃出下方各 except 的异常（CancelledError、未预期 bug 等）
                # 都必须释放半开探测标志，否则该供应商会被 probing 永久卡死。
                try:
                    bucket = self.buckets.get(provider.name)
                    if bucket is not None:
                        max_wait = (provider.proactive_rate_limit.max_wait_seconds
                                    if provider.proactive_rate_limit else 2.0)
                        if not await bucket.acquire(max_wait):
                            self.breaker.release_probe(provider.name)  # 探测者放弃，允许他人再探
                            self.stats.note_soft_saturation(provider.name)
                            self.stats.note_switched_away(provider.name)
                            trace.append((provider.name, "soft_saturated"))
                            continue
                    try:
                        resp = await self._attempt(client, provider, upstream_model, request_body)
                    except ValueError:
                        # SSRF 校验失败：切换下一家
                        self._note_failure(provider, trace, "ssrf")
                        continue
                    except (httpx.HTTPError, OSError):
                        # 网络故障 / DNS·系统错误（getaddrinfo 的 gaierror 属 OSError）：切换下一家
                        self._note_failure(provider, trace, "network")
                        continue
                    kind = classify_status(resp.status_code, resp.snippet)
                    self.stats.record(provider.name, kind)
                    if kind is ErrorKind.OK:
                        self.breaker.record_success(provider.name)  # 能应答即活着（半开探测成功）
                        if resp.stream is not None:
                            # 流式响应：client 所有权移交给流生成器，由其关闭
                            handed_over = True
                        self._log_chat(model, chain_repr, resp, trace, start)
                        return resp
                    if kind is ErrorKind.CLIENT:
                        self.breaker.record_success(provider.name)  # 能应答即活着
                        self._log_chat(model, chain_repr, resp, trace, start)
                        return resp  # 请求问题：透传，不切换
                    self.breaker.record_failure(provider.name, kind)
                    self.stats.note_switched_away(provider.name)
                    trace.append((provider.name, _SWITCH_REASON[kind]))
                except BaseException:
                    # 只保证探测标志释放，不吞异常
                    self.breaker.release_probe(provider.name)
                    raise
        finally:
            if not handed_over:
                await client.aclose()

        result = GatewayResponse(
            502, "application/json", "local",
            body=_openai_error("所有候选供应商当前不可用（熔断或网络故障）",
                               "gateway_error", "all_providers_failed"))
        self._log_chat(model, chain_repr, result, trace, start)
        return result

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
        await asyncio.to_thread(assert_safe_upstream_url, url, resolver=self._resolver)

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
            # aclose 自身可能失败（连接已断），抑制之以免屏蔽原始异常；
            # 此路径未移交所有权，client 由 chat() 的 finally 关闭
            with contextlib.suppress(Exception):
                await response.aclose()
            raise httpx.RemoteProtocolError("upstream closed before first chunk") from None
        except httpx.HTTPError:
            with contextlib.suppress(Exception):
                await response.aclose()
            raise

        async def stream() -> AsyncIterator[bytes]:
            """SSE 响应体：消费方必须完整消费或关闭迭代器（Starlette 的
            StreamingResponse 会做），否则上游连接池泄漏。
            """
            try:
                yield first
                async for chunk in chunks:
                    yield chunk
            except Exception as exc:
                # 流中死亡（GeneratorExit 属 BaseException，不会进此分支）：
                # 记熔断与统计后再透传，避免死供应商仍居最高优先
                self.breaker.record_failure(provider.name, ErrorKind.NETWORK)
                self.stats.note_midstream_error(provider.name)
                logger.warning("stream interrupted model=%s provider=%s err=%s",
                               request_body.get("model"), provider.name, exc)
                raise
            else:
                self.breaker.record_success(provider.name)  # 完整送达：活着
            finally:
                # aclose 自身可能失败（连接已断），抑制之以免屏蔽原始异常
                with contextlib.suppress(Exception):
                    await response.aclose()
                with contextlib.suppress(Exception):
                    await client.aclose()

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
                    await asyncio.to_thread(
                        assert_safe_upstream_url, models_url, resolver=self._resolver)
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
