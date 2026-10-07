"""Merchant admin authentication: Shopify App Bridge session tokens.

The embedded admin page asks App Bridge for a session token (`shopify.idToken()`) and sends it
as `Authorization: Bearer <token>`. Shopify signs it HS256 with this app's client secret and
issues it only to staff of a shop where the app is installed. It is verified here, with no
network call and no stored session:

  * header alg is exactly HS256 (no `none`, no algorithm chosen by the token);
  * signature = HMAC-SHA256(SHOPIFY_API_SECRET), constant-time compare;
  * aud == SHOPIFY_API_KEY (a token minted for another app is refused);
  * exp / nbf checked with a small clock-skew leeway;
  * dest (and iss) must be the configured trusted shop -- a token from any other shop that
    installed the app is refused, exactly like every other Shopify surface in this service.

Never logs the token or its claims beyond the staff user id (`sub`).
"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.config import settings
from app.shopify.trusted_shop import UntrustedShopError, require_trusted_shop

_LEEWAY_SECONDS = 10
_MAX_TOKEN_LENGTH = 4096


class SessionTokenInvalid(Exception):
    """One reason for every failure, so a caller cannot probe which check failed."""


@dataclass(frozen=True)
class MerchantSession:
    shop: str
    user_id: str | None


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _host(url: object) -> str:
    return (urlsplit(url).hostname or "") if isinstance(url, str) else ""


def verify_session_token(token: object, *, now: float | None = None) -> MerchantSession:
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN_LENGTH:
        raise SessionTokenInvalid()
    if not settings.shopify_api_key or not settings.shopify_api_secret:
        raise SessionTokenInvalid()  # fail closed: no configured app identity, no admin access
    parts = token.split(".")
    if len(parts) != 3:
        raise SessionTokenInvalid()
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        signature = _b64url_decode(parts[2])
    except (ValueError, TypeError):
        raise SessionTokenInvalid() from None
    if not isinstance(header, dict) or header.get("alg") != "HS256" or not isinstance(claims, dict):
        raise SessionTokenInvalid()
    expected = hmac.new(settings.shopify_api_secret.encode(), f"{parts[0]}.{parts[1]}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, signature):
        raise SessionTokenInvalid()
    if claims.get("aud") != settings.shopify_api_key:
        raise SessionTokenInvalid()
    now = time.time() if now is None else now
    exp, nbf = claims.get("exp"), claims.get("nbf")
    if not isinstance(exp, (int, float)) or exp + _LEEWAY_SECONDS < now:
        raise SessionTokenInvalid()
    if isinstance(nbf, (int, float)) and nbf - _LEEWAY_SECONDS > now:
        raise SessionTokenInvalid()
    try:
        shop = require_trusted_shop(_host(claims.get("dest")))
        if _host(claims.get("iss")) != shop:
            raise SessionTokenInvalid()
    except UntrustedShopError:
        raise SessionTokenInvalid() from None
    sub = claims.get("sub")
    return MerchantSession(shop=shop, user_id=str(sub) if sub is not None else None)
