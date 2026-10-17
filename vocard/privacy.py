"""No content telemetry or caching for online inference.

Keep this boundary around retrieval AND generation. Request-scoped context variables
are reset in finally so concurrent requests cannot inherit another request's data.
"""

import logging
from contextlib import ExitStack
from functools import wraps

from langchain_core.globals import set_debug, set_verbose
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tracers.context import run_collector_var, tracing_v2_callback_var
from langsmith import tracing_context
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


def configure_private_logging():
    set_debug(False)
    set_verbose(False)
    # SDK debug logs can contain complete HTTP bodies, errors and credentials.
    # This service emits no SDK/access records; counters remain in ai_daily_usage.
    prefixes = ("openai", "httpx", "httpcore", "langchain", "langsmith", "uvicorn.access")
    for name in [*prefixes, *list(logging.root.manager.loggerDict)]:
        if name.startswith(prefixes):
            logger = logging.getLogger(name)
            logger.disabled = True
            logger.setLevel(logging.CRITICAL + 1)


def private_inference(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        configure_private_logging()
        with ExitStack() as stack:
            stack.enter_context(tracing_context(enabled=False, parent=False))
            for var in (var_child_runnable_config, tracing_v2_callback_var, run_collector_var):
                token = var.set(None)
                stack.callback(var.reset, token)
            return function(*args, **kwargs)

    return invoke


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
