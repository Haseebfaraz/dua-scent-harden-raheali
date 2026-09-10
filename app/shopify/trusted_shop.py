"""The single canonical trust boundary for the Shopify shop hostname (Phase 1, finding F1).

This is a single-store application. Exactly one shop may ever receive this app's client
credentials or an Admin access token: the one configured in SHOPIFY_SHOP_DOMAIN. Every layer that
builds a credential-bearing Shopify URL (app/shopify/admin_auth.py, app/shopify/admin_client.py)
calls require_trusted_shop() itself, so a caller that forgets to validate cannot bypass it, and
an untrusted value fails closed BEFORE any HTTP request is made.

Rules, in order:
  * the configured shop is mandatory (no hard-coded fallback, in any environment);
  * the candidate must be a bare ASCII hostname: no scheme, userinfo, port, path, query,
    fragment, whitespace, or non-ASCII characters at all (so no IDNA/homoglyph folding applies);
  * it must be a syntactically valid `<store>.myshopify.com` hostname (IP literals, localhost,
    custom domains, suffix/subdomain tricks all fail this);
  * and it must equal the configured shop exactly (compared with a constant-time comparison).

Nothing here logs or echoes the candidate value: an attacker-controlled hostname must not be
written into log lines or error messages that could reach a customer.
"""

import hmac
import re

from app.config import settings

_MYSHOPIFY_SUFFIX = ".myshopify.com"
# One DNS label (1-63 chars, letters/digits/hyphens, no leading/trailing hyphen) followed by the
# exact myshopify.com suffix. Anchored on both ends so nothing can be appended or prepended.
_SHOP_HOSTNAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.myshopify\.com$")
_FORBIDDEN_CHARACTERS = frozenset("/\\@:?#[]<>\"' \t\r\n")
_MAX_HOSTNAME_LENGTH = 253


class UntrustedShopError(Exception):
    """The supplied shop is not the configured trusted shop. Never carries the offending value."""


class TrustedShopNotConfigured(UntrustedShopError):
    """SHOPIFY_SHOP_DOMAIN is unset or invalid. Admin API usage is refused until it is fixed."""


def canonicalize_shop_hostname(value: object) -> str:
    """Return the canonical lowercase ASCII form of a `<store>.myshopify.com` hostname, or raise
    UntrustedShopError. Pure syntax; does NOT check the allowlist (see require_trusted_shop)."""
    if not isinstance(value, str) or not value:
        raise UntrustedShopError("shop hostname missing")
    if len(value) > _MAX_HOSTNAME_LENGTH:
        raise UntrustedShopError("shop hostname too long")
    if not value.isascii() or not value.isprintable():
        # A myshopify.com hostname is always plain ASCII. Rejecting any non-ASCII input outright
        # closes every Unicode/IDNA trick at once: homoglyphs, fullwidth forms, and NFKC
        # look-alikes that IDNA nameprep would otherwise silently fold into the real hostname.
        raise UntrustedShopError("shop hostname contains forbidden characters")
    if any(ch in _FORBIDDEN_CHARACTERS for ch in value):
        # scheme ("https://"), userinfo ("user:pass@"), port (":443"), path, query, fragment,
        # brackets (IPv6 literal), quotes, whitespace.
        raise UntrustedShopError("shop hostname contains forbidden characters")
    if value.endswith(".") or value.startswith("."):
        raise UntrustedShopError("shop hostname malformed")
    canonical = value.lower()
    if not _SHOP_HOSTNAME_PATTERN.fullmatch(canonical):
        raise UntrustedShopError("shop hostname is not a valid myshopify.com hostname")
    return canonical


def trusted_shop() -> str:
    """The one configured, canonicalized shop. Raises TrustedShopNotConfigured if unset/invalid."""
    configured = settings.shopify_shop_domain
    if not configured:
        raise TrustedShopNotConfigured("SHOPIFY_SHOP_DOMAIN is not configured")
    try:
        return canonicalize_shop_hostname(configured)
    except UntrustedShopError:
        raise TrustedShopNotConfigured("SHOPIFY_SHOP_DOMAIN is not a valid myshopify.com hostname") from None


def require_trusted_shop(value: object) -> str:
    """Return the canonical trusted shop iff `value` canonicalizes to exactly that shop.
    Raises UntrustedShopError (or TrustedShopNotConfigured) otherwise. Fail closed."""
    expected = trusted_shop()
    candidate = canonicalize_shop_hostname(value)
    if not hmac.compare_digest(candidate.encode("ascii"), expected.encode("ascii")):
        raise UntrustedShopError("shop is not the trusted configured shop")
    return expected


def is_trusted_shop(value: object) -> bool:
    try:
        require_trusted_shop(value)
        return True
    except UntrustedShopError:
        return False
