"""HTTP privacy controls shared by lightweight chat and the full API."""

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def configure_private_logging():
    # SDK debug logs can contain complete HTTP bodies, errors and credentials.
    # This service emits no SDK/access records; counters remain in ai_daily_usage.
    prefixes = ("openai", "httpx", "httpcore", "langchain", "langsmith", "uvicorn.access")
    for name in [*prefixes, *list(logging.root.manager.loggerDict)]:
        if name.startswith(prefixes):
            logger = logging.getLogger(name)
            logger.disabled = True
            logger.setLevel(logging.CRITICAL + 1)


class PrivateRequestBoundary(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        private = request.url.path.startswith("/ai/") or request.url.path.endswith(
            ("/answer", "/chat")
        )
        if not private:
            return await call_next(request)
        try:
            if request.query_params:
                response = JSONResponse(
                    {"detail": "Question parameters belong in the JSON body"}, status_code=422
                )
            else:
                response = await call_next(request)
            if response.status_code == 422:
                # FastAPI's default validation response echoes rejected input.
                response = JSONResponse({"detail": "Invalid question request"}, status_code=422)
        except Exception:
            # Do not expose/log exceptions with provider bodies or request locals.
            response = JSONResponse({"detail": "AI service unavailable"}, status_code=503)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return response
