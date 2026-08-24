from pydantic import BaseModel


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = ""
    customer_email: str | None = None
    customer_name: str | None = None
    greeting: str | None = None
    # Required from Node (which already resolved it); absent from a direct storefront call, in
    # which case the /chat route resolves it itself via app.shopify.sessions.resolve_shop_domain.
    shop_domain: str | None = None
