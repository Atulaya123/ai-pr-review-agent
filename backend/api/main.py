import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from backend.api.reviews import router as reviews_router
from backend.core.config import get_settings
from backend.mcp_server.server import build_mcp
from backend.webhook_receiver.router import router as webhook_router


def create_app() -> FastAPI:
    # Built before the FastAPI app itself so its session_manager.run() can be
    # entered by the app's own lifespan — see build_mcp's docstring for why a
    # bare app.mount() alone leaves the MCP session manager's task group
    # uninitialized and every /mcp request 500ing.
    mounted = build_mcp(get_settings())

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if mounted is None:
            yield
            return
        _, mcp = mounted
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="AI PR Review Agent", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.include_router(webhook_router)
    app.include_router(reviews_router)

    # /mcp is only mounted when MCP_SHARED_SECRET is actually set — see
    # build_mcp's docstring for why this fails closed instead of defaulting
    # to an open, unauthenticated endpoint.
    if mounted is not None:
        mcp_app, _ = mounted
        app.mount("/mcp", mcp_app)

    return app


app = create_app()
