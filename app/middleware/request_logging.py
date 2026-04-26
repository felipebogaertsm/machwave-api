from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


class LoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: object) -> Response:
        start = time.perf_counter()
        response: Response = await call_next(request)  # type: ignore[operator]
        duration_ms = (time.perf_counter() - start) * 1000
        msg = "%s %s %s (%.0fms)"
        args = (request.method, request.url.path, response.status_code, duration_ms)
        if response.status_code >= 400:
            logger.error(msg, *args)
        else:
            logger.info(msg, *args)
        return response
