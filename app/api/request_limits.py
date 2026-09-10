"""Application-level input limits for the public chat surface (Phase 2, finding F6 / G).

These run at the schema/handler layer BEFORE any database, OpenAI, or tool work, independently
of any web-server body limit. Customer-safe messages only; raw input is never echoed.
"""

import re
import unicodedata

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings

# Conversation ids this service minted are 32 hex chars; legacy Node-era ids are cuid-shaped.
_CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_ALLOWED_CONTROL = {"\n", "\r", "\t"}


class InvalidChatInput(ValueError):
    """Customer-safe message; never echoes the raw input."""


def validate_conversation_id(raw: object) -> str:
    if not isinstance(raw, str) or not _CONVERSATION_ID_PATTERN.match(raw):
        raise InvalidChatInput("Invalid conversation reference.")
    return raw


def validate_token_shape(raw: object) -> str | None:
    """Shape check only (a malformed token is rejected before any lookup). None when absent."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not _TOKEN_PATTERN.match(raw):
        raise InvalidChatInput("Invalid session token.")
    return raw


def validate_chat_message(raw: object) -> str:
    if not isinstance(raw, str):
        raise InvalidChatInput("Message must be text.")
    if len(raw) > settings.chat_max_message_chars:
        raise InvalidChatInput(f"Messages are limited to {settings.chat_max_message_chars} characters.")
    normalized = unicodedata.normalize("NFC", raw)
    if any((unicodedata.category(ch) == "Cc" and ch not in _ALLOWED_CONTROL) or ch == "￾" or ch == "￿" for ch in normalized):
        raise InvalidChatInput("Message contains characters that cannot be used.")
    stripped = normalized.strip()
    if not stripped:
        raise InvalidChatInput("Message cannot be empty.")
    return stripped


class RequestBodyLimitMiddleware:
    """Refuse JSON API request bodies above MAX_REQUEST_BODY_BYTES with 413 before reading them.
    Pure ASGI (no BaseHTTPMiddleware) so streaming responses are untouched. A body with no
    Content-Length and chunked transfer is refused (411) on the limited paths: every legitimate
    caller (browser fetch, Node adapter) sends a Content-Length."""

    def __init__(self, app: ASGIApp, *, limited_path_prefixes: tuple[str, ...] = ("/chat", "/internal/chat", "/api/", "/apps/scent-library/fragrance-preview")):
        self.app = app
        self.limited_path_prefixes = limited_path_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if not any(path == p or path.startswith(p) for p in self.limited_path_prefixes):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        content_length = headers.get("content-length")
        if content_length is None:
            if "chunked" in headers.get("transfer-encoding", "").lower():
                await _plain_response(send, 411, b'{"error":"Length required."}')
                return
            await self.app(scope, receive, send)
            return
        try:
            length = int(content_length)
        except ValueError:
            await _plain_response(send, 400, b'{"error":"Invalid request."}')
            return
        if length > settings.max_request_body_bytes:
            await _plain_response(send, 413, b'{"error":"Request too large."}')
            return
        await self.app(scope, receive, send)


async def _plain_response(send: Send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})
