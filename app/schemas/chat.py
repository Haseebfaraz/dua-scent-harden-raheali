from typing import Any

from pydantic import BaseModel, ConfigDict


class ChatRequest(BaseModel):
    # Unknown keys are ignored (not rejected) so older widgets that still send retired fields
    # keep working; the retired fields below are accepted and IGNORED, never acted on.
    model_config = ConfigDict(extra="ignore")

    # Identifier only. Continuing an existing conversation also requires the conversation
    # capability (X-Conversation-Token header or `conversation_token`). Omit both to start a new
    # conversation; the stream's `id` event then carries the new id and token.
    conversation_id: str | None = None
    conversation_token: str | None = None
    # Validated by app/api/request_limits.py (length, control characters) before any work.
    message: Any = None
    # SELF-REPORTED contact data (see app/services/customer_identity.py). Never authentication.
    customer_email: Any = None
    customer_name: Any = None
    # Server-owned welcome line on a brand-new conversation (replaces the retired `greeting`).
    with_welcome: bool = False
    # RETIRED (Phase 2, N2): browser-supplied assistant text is ignored.
    greeting: Any = None
    # RETIRED (Phase 1, F1): the shop is always the configured trusted shop.
    shop_domain: Any = None


class ChatSessionRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    with_welcome: bool = False
