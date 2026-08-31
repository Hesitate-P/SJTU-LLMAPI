"""FastAPI 入口：本地 OpenAI 兼容端点。"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import AppConfig, load_config
from .failover import Breaker
from .forward import GatewayResponse, GatewayService
from .ratelimit import TokenBucket
from .stats import Stats

# uvicorn 只配置自身 logger，root logger 无 handler 时 gateway 的 INFO 日志在生产部署下不会输出。
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

logger = logging.getLogger("gateway")


def _build_service(cfg: AppConfig) -> GatewayService:
    breaker = Breaker(
        cfg.failover.cooldown_429_seconds,
        cfg.failover.cooldown_quota_seconds,
        cfg.failover.cooldown_network_seconds,
    )
    buckets: dict[str, TokenBucket] = {}
    for p in cfg.providers:
        if p.proactive_rate_limit:
            buckets[p.name] = TokenBucket(
                p.proactive_rate_limit.requests_per_minute,
                p.proactive_rate_limit.burst,
            )
    return GatewayService(cfg, breaker, Stats(), buckets)


def create_app(cfg: AppConfig | None = None, service: GatewayService | None = None) -> FastAPI:
    cfg = cfg or load_config(os.environ.get("GATEWAY_CONFIG", "config.yaml"))
    service = service or _build_service(cfg)
    app = FastAPI(title="sjtu-llm-gateway")

    @app.middleware("http")
    async def require_gateway_key(request: Request, call_next):
        # /health 供运维探针免鉴权访问；仅暴露可用性状态
        if request.url.path == "/health":
            return await call_next(request)
        expected = os.environ.get("GATEWAY_API_KEY")
        if expected:
            supplied = request.headers.get("authorization", "")
            if supplied != f"Bearer {expected}":
                return JSONResponse(status_code=401, content={
                    "error": {"message": "无效的网关密钥",
                              "type": "invalid_request_error"}})
        return await call_next(request)

    def _to_response(result: GatewayResponse) -> Response:
        headers = {"X-Gateway-Provider": result.provider}
        if result.stream is not None:
            return StreamingResponse(result.stream, status_code=result.status_code,
                                     media_type=result.media_type, headers=headers)
        return Response(content=result.body, status_code=result.status_code,
                        media_type=result.media_type, headers=headers)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        body = await request.json()
        return _to_response(await service.chat(body))

    @app.get("/v1/models")
    async def models() -> Response:
        return _to_response(await service.list_models())

    @app.get("/health")
    async def health() -> dict:
        providers = {}
        for p in cfg.providers:
            providers[p.name] = {
                "available": p.available,
                "breaker_open": service.breaker.is_open(p.name),
                "cooldown_remaining": round(service.breaker.cooldown_remaining(p.name), 1),
                "unavailable_reason": p.unavailable_reason or None,
            }
        return {"status": "ok", "providers": providers}

    @app.get("/stats")
    async def stats() -> dict:
        return service.stats.snapshot()

    # 启动摘要：只记 provider 名与可用性（不可用时为原因），不含任何密钥值
    logger.info("网关启动：providers=%s",
                [(p.name, "可用" if p.available else p.unavailable_reason)
                 for p in cfg.providers])
    return app


_config_path = os.environ.get("GATEWAY_CONFIG", "config.yaml")
if os.path.exists(_config_path):
    app = create_app()  # 生产/容器：config.yaml 已挂载
else:
    app = None  # 本地开发未提供配置时允许导入（测试显式传 cfg）；uvicorn 启动需先备好配置


if __name__ == "__main__":
    # python -m app.main 独立运行：接线 config.yaml 的 listen_host/listen_port
    # （容器内仍走 Dockerfile CMD，不受影响）
    import uvicorn

    cfg = load_config(_config_path)
    uvicorn.run(app or create_app(cfg), host=cfg.listen_host, port=cfg.listen_port)
