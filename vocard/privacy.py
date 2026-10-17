"""No content telemetry or caching for online inference.

Keep this boundary around retrieval AND generation. Request-scoped context variables
are reset in finally so concurrent requests cannot inherit another request's data.
"""

from contextlib import ExitStack
from functools import wraps

from langchain_core.globals import set_debug, set_verbose
from langchain_core.runnables.config import var_child_runnable_config
from langchain_core.tracers.context import run_collector_var, tracing_v2_callback_var
from langsmith import tracing_context

from vocard.http_privacy import PrivateRequestBoundary  # noqa: F401
from vocard.http_privacy import configure_private_logging as configure_http_private_logging


def configure_private_logging():
    set_debug(False)
    set_verbose(False)
    configure_http_private_logging()

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
