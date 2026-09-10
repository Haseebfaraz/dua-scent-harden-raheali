"""Two identities that must never be confused (Phase 2, finding F8).

SelfReportedIdentity
    What the customer (or the caller on their behalf, or the model extracting from chat) SAYS
    their name/email is. Profile and contact data. Validated for shape and length, never for
    truth. It authenticates nothing, grants access to nothing, and never overwrites a verified
    binding.

VerifiedShopifyCustomer
    A `logged_in_customer_id` that arrived inside a Shopify App Proxy query string whose
    signature was verified (app/shopify/app_proxy.py). The signature proves Shopify supplied
    the value; it does NOT prove that any particular conversation or recommendation belongs
    to that customer -- ownership is still checked object by object.

Precedence when both exist for the same fact: a verified binding always wins and is never
replaced; self-reported values only fill a field that is still empty.

The public /chat route has no Shopify session at all (it is a direct storefront fetch, not an
App Proxy request), so everything it receives about the customer is self-reported.
"""

import re
import unicodedata
from dataclasses import dataclass

from app.config import settings

_EMAIL_PATTERN = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_CUSTOMER_ID_PATTERN = re.compile(r"^[0-9]{1,32}$")
_REJECTED_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


@dataclass(frozen=True)
class SelfReportedIdentity:
    name: str | None = None
    email: str | None = None


@dataclass(frozen=True)
class VerifiedShopifyCustomer:
    customer_id: str


def clean_self_reported_name(raw: object) -> str | None:
    """Trim/normalize a claimed name; None if absent or unusable. Never raises for bad input:
    a bad claimed name is simply not a name, it is never an error a caller can probe with."""
    if not isinstance(raw, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFC", raw).split())
    if not normalized or len(normalized) > settings.chat_max_name_chars:
        return None
    if any(unicodedata.category(ch) in _REJECTED_CATEGORIES for ch in normalized):
        return None
    return normalized


def clean_self_reported_email(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate or len(candidate) > settings.chat_max_email_chars or not _EMAIL_PATTERN.match(candidate):
        return None
    return candidate


def self_reported_identity(name: object, email: object) -> SelfReportedIdentity:
    return SelfReportedIdentity(name=clean_self_reported_name(name), email=clean_self_reported_email(email))


def verified_shopify_customer_from_signed_params(params: dict) -> VerifiedShopifyCustomer | None:
    """Only ever call with parameters that have ALREADY passed the App Proxy signature check.
    Shopify sends an empty string for guests."""
    raw = params.get("logged_in_customer_id")
    if not isinstance(raw, str) or not raw:
        return None
    if not _CUSTOMER_ID_PATTERN.match(raw):
        return None
    return VerifiedShopifyCustomer(customer_id=raw)
