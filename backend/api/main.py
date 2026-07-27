from fastapi import FastAPI

from backend.api.reviews import router as reviews_router
from backend.webhook_receiver.router import router as webhook_router


def create_app() -> FastAPI:
    app = FastAPI(title="AI PR Review Agent")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.include_router(webhook_router)
    app.include_router(reviews_router)
    return app


app = create_app()
