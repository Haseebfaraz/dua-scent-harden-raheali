from pydantic import BaseModel


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    message: str = ""
    customer_email: str | None = None
    customer_name: str | None = None
    greeting: str | None = None
    shop_domain: str
