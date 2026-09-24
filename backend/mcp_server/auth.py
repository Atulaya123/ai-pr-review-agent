"""Phase 1 auth: a single shared-secret bearer check in front of the MCP
mount. server.py fails closed if no secret is configured (skips mounting
entirely) — this middleware only has to handle the "secret is set" case.

Phase 2 replaces this for the review_diff tool specifically with OAuth 2.1
token validation against GitHub's own authorization server (PKCE,
audience-bound tokens) — this shared secret stays in front of the two
read-only tools either way, it's cheap and there's no reason to remove it.
"""

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


class SharedSecretMiddleware:
    def __init__(self, app: ASGIApp, secret: str) -> None:
        self.app = app
        self.secret = secret

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            # Lifespan events carry the MCP session manager's startup/shutdown
            # (see streamable_http_app's lifespan=lambda app: session_manager.run()
            # in the mcp SDK) — must pass through untouched, or the session
            # manager never starts and every real request 500s.
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        if request.headers.get("authorization") != f"Bearer {self.secret}":
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)
