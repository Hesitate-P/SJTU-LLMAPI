from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="sjtu-llm-gateway")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
