from pydantic import BaseModel


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = ""
    customer_email: str | None = None
    customer_name: str | None = None
    greeting: str | None = None
    # Accepted for wire compatibility with the Node adapter only. Phase 1 (security): the value
    # is IGNORED -- the shop is always the configured trusted shop (app/shopify/trusted_shop.py).
    shop_domain: str | None = None
