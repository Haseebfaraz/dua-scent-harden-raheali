"""Structured, production-friendly logging. Every line is one JSON object: timestamp, level,
logger name, message, and a request_id when the log happened inside a request (set by
RequestContextMiddleware below). Never log OpenAI/DB/Odoo credentials or full auth headers --
every call site in this codebase that logs a request already hand-picks safe fields rather than
dumping headers/payloads wholesale (see odoo_inventory.py's own comment on this).
"""

import json
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = _request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request_id (visible on every log line emitted while handling this request) and
    logs one summary line per request: method, path, status, duration_ms.
    """

    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        token = _request_id_var.set(request_id)
        started_at = time.monotonic()
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        duration_ms = round((time.monotonic() - started_at) * 1000, 1)
        logging.getLogger("app.request").info(
            "%s %s -> %s (%sms) [%s]",
            request.method, request.url.path, response.status_code, duration_ms, request_id,
        )
        response.headers["X-Request-Id"] = request_id
        return response
