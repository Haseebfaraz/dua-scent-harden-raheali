"""Port of app/routes/api.webhooks.jsx -- only APP_UNINSTALLED is handled; any other topic is a
404, matching the reference app's own `default: throw new Response(..., {status: 404})`.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Session
from app.db.session import get_session
from app.shopify.hmac import verify_webhook_hmac

router = APIRouter()


@router.post("/shopify/webhooks")
async def handle_webhook(
    request: Request,
    x_shopify_topic: str | None = Header(default=None),
    x_shopify_shop_domain: str | None = Header(default=None),
    x_shopify_hmac_sha256: str | None = Header(default=None),
    db_session: AsyncSession = Depends(get_session),
) -> dict:
    raw_body = await request.body()
    if not verify_webhook_hmac(raw_body, x_shopify_hmac_sha256, settings.shopify_api_secret):
        raise HTTPException(status_code=401, detail="invalid webhook signature")

    if x_shopify_topic == "app/uninstalled":
        await db_session.execute(delete(Session).where(Session.shop == x_shopify_shop_domain))
        await db_session.commit()
        return {}

    raise HTTPException(status_code=404, detail="unhandled webhook topic")
